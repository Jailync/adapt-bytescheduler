// Copyright 2019 Bytedance Inc. All Rights Reserved.
// Copyright 2018 Uber Technologies, Inc. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
// =============================================================================

#if HAVE_CUDA
#include <c10/cuda/CUDAStream.h>
#include <c10/cuda/CUDAException.h>
#endif

#include <torch/extension.h>
#include <torch/torch.h>
#include <chrono>
#include <memory>
#include <thread>

#include "../common/operations.h"
#include "../common/local_operations.h"
#include "../common/logging.h"
#include "adapter.h"
#include "ops.h"
#include "cuda_util.h"
#include "handle_manager.h"
#include "ready_event.h"

#define DC_BLOCK_SIZE (256)
#define DC_GRID_SIZE (32)
#define DC_GROUP_SIZE (64)

namespace byteps {
namespace torch {

static HandleManager handle_manager;

namespace {

std::string GetOpName(const std::string& prefix, const std::string& name,
                      int handle) {
  if (!name.empty()) {
    return prefix + "." + std::string(name);
  }
  return prefix + ".noname." + std::to_string(handle);
}

int GetDeviceID(const ::torch::Tensor& tensor) {
  if (tensor.device().is_cuda()) {
    return tensor.device().index();
  }
  return CPU_DEVICE_ID;
}

}  // namespace

enum TaskType {
  kSend,
  kRecv,
  kPushPull,
  kAllgather,
};

// For recv, the tensor is the output
// For send, the tensor is the input
void StartP2PTask(::torch::Tensor tensor, int sender, int receiver,
                  const std::string tensor_name, int version, int priority,
                  int handle, TaskType task) {
  auto device = GetDeviceID(tensor);
  auto ready_event = RecordReadyEvent(device);
  auto tensor_ptr = std::make_shared<TorchTensor>(tensor);
  auto byteps_input = task == kSend ? tensor_ptr : nullptr;
  auto byteps_output = task == kRecv ? tensor_ptr : nullptr;
  if (task == kSend && receiver == byteps_rank()) {
    byteps_output = tensor_ptr;
  }
  if (task == kRecv && sender == byteps_rank()) {
    byteps_input = tensor_ptr;
  }
  size_t size = tensor_ptr->size();
  auto dtype = tensor_ptr->dtype();

  auto& context = common::GetContextFromName(tensor_name);
  common::InitTensorP2P(context, size, dtype,
                        (device == CPU_DEVICE_ID)
                        ? const_cast<void*>(tensor_ptr->data())
                        : nullptr, sender, receiver, false);

  std::shared_ptr<std::vector<QueueType>> queue_list;
  if (task == kSend) {
    queue_list = common::GetSendQueueList();
  } else if (task == kRecv) {
    queue_list = common::GetRecvQueueList();
  } else {
    BPS_CHECK(false) << "unexpected task=" << task;
  }

  auto enqueue_result = common::EnqueueTensor(
      context, byteps_input, byteps_output, ready_event, device, priority,
      version,
      [handle, tensor](const Status& status) mutable {
        handle_manager.MarkDone(handle, status);
      },
      queue_list);

  ThrowIfError(enqueue_result);
  return;
}

void StartTask(::torch::Tensor tensor, ::torch::Tensor output, int average,
               const std::string tensor_name, int version, int priority,
               int handle) {

  auto device = GetDeviceID(tensor);
  auto ready_event = RecordReadyEvent(device);
  auto byteps_input = std::make_shared<TorchTensor>(tensor);
  auto byteps_output = std::make_shared<TorchTensor>(output);
  size_t size = byteps_input->size();
  auto dtype = byteps_input->dtype();

  auto& context = common::GetContextFromName(tensor_name);
  common::InitTensor(context, size, dtype,
                      (device == CPU_DEVICE_ID)
                      ? const_cast<void*>(byteps_input->data())
                      : nullptr);

  // kPushPull
  auto queue_list = common::GetPushQueueList(device);
  auto queue_list_pull = common::GetPullQueueList(device);
  queue_list->insert(queue_list->end(), queue_list_pull->begin(),
                     queue_list_pull->end());

  auto enqueue_result = common::EnqueueTensor(
      context, byteps_input, byteps_output, ready_event, device, priority,
      version,
      [handle, average, tensor, output](const Status& status) mutable {
        // Will execute in the `device` context.
        if (average) {
          if (isIntegralType(output.scalar_type())) {
            output.floor_divide_(byteps_size());
            handle_manager.MarkDone(handle, status);
            return;
          }
          output.div_(byteps_size());
        }
        handle_manager.MarkDone(handle, status);
      },
      queue_list);

  ThrowIfError(enqueue_result);
  return;

}

void StartAllgatherTask(::torch::Tensor tensor, ::torch::Tensor output,
                        const std::vector<int>& shape_list,
                        const std::string tensor_name, int version, 
                        int priority, int handle) {
  auto& context = common::GetContextFromName(tensor_name);                       
  auto device = GetDeviceID(tensor);
  auto byteps_input = std::make_shared<TorchTensor>(tensor);
  auto byteps_output = std::make_shared<TorchTensor>(output);
  size_t input_size = byteps_input->size();
  auto dtype = byteps_input->dtype();
  size_t output_size = byteps_output->size();

  common::InitTensorAllgather(context, input_size, output_size, dtype,
                             (device == CPU_DEVICE_ID)
                             ? const_cast<void*>(byteps_input->data())
                             : nullptr);

  auto ready_event = RecordReadyEvent(device);
  auto enqueue_result = 
    common::EnqueueAllgatherTensor(
      context, byteps_input, byteps_output, ready_event, device, priority, 
      version, shape_list,
      [handle, tensor, output](const Status& status) mutable {
        handle_manager.MarkDone(handle, status);
      });

  ThrowIfError(enqueue_result);
  return;
}

int DoRecv(::torch::Tensor tensor, int sender, int receiver,
           const std::string& name, int version, int priority) {
  ThrowIfError(common::CheckInitialized());

  auto handle = handle_manager.AllocateHandle();
  std::string prefix = "byteps_p2p_send_";
  if (sender == -1) sender = byteps_rank();
  if (receiver == -1) receiver = byteps_rank();
  prefix += std::to_string(sender) + "_recv_" + std::to_string(receiver);
  std::string tensor_name = GetOpName(prefix, name.c_str(), 0);
  auto& context = common::GetContextFromName(tensor_name);
  if (context.initialized) {
    StartP2PTask(tensor, sender, receiver, tensor_name, version, priority, handle, kRecv);
  } else {
    std::thread t(StartP2PTask, tensor, sender, receiver, tensor_name, version, priority, handle, kRecv);
    t.detach();
  }
  return handle;
}


int DoSend(::torch::Tensor tensor, int sender, int receiver,
           const std::string& name, int version, int priority) {
  ThrowIfError(common::CheckInitialized());

  auto handle = handle_manager.AllocateHandle();
  std::string prefix = "byteps_p2p_send_";
  if (sender == -1) sender = byteps_rank();
  if (receiver == -1) receiver = byteps_rank();
  prefix += std::to_string(sender) + "_recv_" + std::to_string(receiver);
  std::string tensor_name = GetOpName(prefix, name.c_str(), 0);
  auto& context = common::GetContextFromName(tensor_name);
  if (context.initialized) {
    StartP2PTask(tensor, sender, receiver, tensor_name, version, priority, handle, kSend);
  } else {
    std::thread t(StartP2PTask, tensor, sender, receiver, tensor_name, version, priority, handle, kSend);
    t.detach();
  }
  return handle;
}

int DoPushPull(::torch::Tensor tensor, ::torch::Tensor output, int average,
               const std::string& name, int version, int priority, int staleness) {
  ThrowIfError(common::CheckInitialized());
  auto handle = handle_manager.AllocateHandle();
  std::string tensor_name;
  if (staleness == 0) {
    tensor_name = GetOpName("byteps", name.c_str(), 0);
  } else {
    std::string tmp_name = "byteps_version_" + std::to_string(version);
    tensor_name = GetOpName(tmp_name, name.c_str(), 0);
  }
  auto& context = common::GetContextFromName(tensor_name);
  if (context.initialized) {
    StartTask(tensor, output, average, tensor_name, version, priority, handle);
  } else {
    std::thread t(StartTask, tensor, output, average, tensor_name, version, priority, handle);
    t.detach();
  }
  return handle;
}

int DoAllgather(::torch::Tensor tensor, ::torch::Tensor output, const std::vector<int>& shape_list, 
                const std::string& name, int version, int priority, int staleness) {
  ThrowIfError(common::CheckInitialized());

  auto handle = handle_manager.AllocateHandle();
  std::string tensor_name;
  if (staleness == 0) {
    tensor_name = GetOpName("byteps", name.c_str(), 0);
  } else {
    std::string tmp_name = "byteps_version_" + std::to_string(version);
    tensor_name = GetOpName(tmp_name, name.c_str(), 0);
  }
  auto& context = common::GetContextFromName(tensor_name);
  if (context.initialized) {
    StartAllgatherTask(tensor, output, shape_list, tensor_name, version, priority, handle);
  } else {
    std::thread t(StartAllgatherTask, tensor, output, shape_list, tensor_name, version, priority, handle);
    t.detach();
  }
  return handle;
}

void SetNumGrads(int num_grads) {
  std::lock_guard<std::mutex> lock(mutex_);
  num_grads_ = num_grads;
  grad_count_ = 0;
  return;
}

int PollHandle(int handle) { return handle_manager.PollHandle(handle) ? 1 : 0; }

void DeclareTensor(const std::string& name, int staleness) {
  int num_versions = staleness + 1;
  if (num_versions == 1) {
    std::string tensor_name = GetOpName("byteps", name.c_str(), 0);
    common::DeclareTensor(tensor_name, -1);
  } else {
    for (int i=0; i < num_versions; ++i) {
      std::string tmp_name = "byteps_version_" + std::to_string(i);
      std::string tensor_name = GetOpName(tmp_name, name.c_str(), 0);
      common::DeclareTensor(tensor_name, -1);
    }
  }
}

void DeclareTensorP2P(const std::string& name, int sender, int receiver) {
  std::string prefix = "byteps_p2p_send_";
  if (sender == -1) sender = byteps_rank();
  if (receiver == -1) receiver = byteps_rank();
  prefix += std::to_string(sender) + "_recv_" + std::to_string(receiver);
  std::string tensor_name = GetOpName(prefix, name.c_str(), 0);
  common::DeclareP2PTensor(tensor_name, sender, receiver);
}

void DeclareTensorAllgather(const std::string& name, int staleness) {
  int num_versions = staleness + 1;
  if (num_versions == 1) {
    std::string tensor_name = GetOpName("byteps", name.c_str(), 0);
    common::DeclareAllgatherTensor(tensor_name, -1);
  } else {
    for (int i=0; i < num_versions; ++i) {
      std::string tmp_name = "byteps_version_" + std::to_string(i);
      std::string tensor_name = GetOpName(tmp_name, name.c_str(), 0);
      common::DeclareAllgatherTensor(tensor_name, -1);
    }
  }
}

void WaitAndClear(int handle, bool busy_waiting) {
  while (true) {
    if (handle_manager.PollHandle(handle)) break;
    {
      py::gil_scoped_release release_python_gil;
      if (!busy_waiting) {
        std::this_thread::sleep_for(std::chrono::microseconds(1));
      } else {
        std::this_thread::yield();
      }
    }
  }
  auto status = handle_manager.ReleaseHandle(handle);
  ThrowIfError(*status);
}

pybind11::tuple DoPushPullGroupSync(::torch::Tensor tensor,
                                    ::torch::Tensor output, int average,
                                    const std::string& name, int version,
                                    int priority, int staleness) {
  ThrowIfError(common::CheckInitialized());

  auto handle = handle_manager.AllocateHandle();
  std::string tensor_name;
  if (staleness == 0) {
    tensor_name = GetOpName("byteps", name.c_str(), 0);
  } else {
    std::string tmp_name = "byteps_version_" + std::to_string(version);
    tensor_name = GetOpName(tmp_name, name.c_str(), 0);
  }
  auto& context = common::GetContextFromName(tensor_name);
  int curr_count;

  if (context.initialized) {
    StartTask(tensor, output, average, tensor_name, version, priority, handle);
  } else {
    std::thread t(StartTask, tensor, output, average, tensor_name, version,
                  priority, handle);
    t.detach();
  }

  {
    std::lock_guard<std::mutex> lock(mutex_);
    grad_count_++;
    curr_count = grad_count_;
    if (grad_count_ == num_grads_) {
      grad_count_ = 0;
    }
  }

  return pybind11::make_tuple(handle, curr_count);
}

#if HAVE_CUDA
int BatchedFuse(const std::vector<::torch::Tensor> input_tensors,
  ::torch::Tensor fused_output_tensor) {
  // check len(src) <= len(dst)
  std::vector<std::shared_ptr<Tensor>> src;
  size_t total_len = 0;
  char *dst;

  for (size_t i = 0; i < input_tensors.size(); i++) {
    auto bps_src = std::make_shared<TorchTensor>(input_tensors[i]);
    total_len += bps_src->size();
    src.push_back(std::move(bps_src));
  }

  auto bps_dst = std::make_shared<TorchTensor>(fused_output_tensor);
  dst = (char *) bps_dst->data();
  size_t dst_len = bps_dst->size();
  if (total_len > dst_len) {
    std::cerr << "!!!!! total_len " << total_len << " dst_len " << dst_len << std::endl;
    raise(SIGSEGV);
  }
  auto curr_stream = c10::cuda::getCurrentCUDAStream();
  MemcpyInFusionBuffer(src, dst, curr_stream);
  return 0;
}

int BatchedUnfuse(const ::torch::Tensor fused_input_tensor,
  std::vector<::torch::Tensor> output_tensors) {
  // check len(src) <= len(dst)
  std::vector<std::shared_ptr<Tensor>> dst;
  size_t total_len = 0;
  char *src;

  for (size_t i = 0; i < output_tensors.size(); i++) {
    auto bps_dst = std::make_shared<TorchTensor>(output_tensors[i]);
    total_len += bps_dst->size();
    dst.push_back(std::move(bps_dst));
  }

  auto bps_src = std::make_shared<TorchTensor>(fused_input_tensor);
  size_t src_len = bps_src->size();
  src = (char *) bps_src->data();
  if ((int) total_len > (int) src_len) {
    std::cerr << "!!!!! total_len " << total_len << " src_len " << src_len << std::endl;
    raise(SIGSEGV);
  }
  auto curr_stream = c10::cuda::getCurrentCUDAStream();
  cudaStream_t curr_stream_t = curr_stream.stream();
  MemcpyOutFusionBuffer(src, dst, curr_stream_t);
  return 0;
}


int BatchedZeroOut(std::vector<::torch::Tensor> output_tensors) {
  // check len(src) <= len(dst)
  std::vector<std::shared_ptr<Tensor>> dst;
  size_t total_len = 0;

  for (size_t i = 0; i < output_tensors.size(); i++) {
    auto bps_dst = std::make_shared<TorchTensor>(output_tensors[i]);
    total_len += bps_dst->size();
    dst.push_back(std::move(bps_dst));
  }

  auto curr_stream = c10::cuda::getCurrentCUDAStream();
  cudaStream_t curr_stream_t = curr_stream.stream();
  ZeroOutTensors(dst, curr_stream_t);
  return 0;
}


int DelayCompensation(std::vector<::torch::Tensor> params, std::vector<::torch::Tensor> grads,
                      std::vector<::torch::Tensor> prev_params, float lambda) {
  std::vector<std::shared_ptr<Tensor>> d_params, d_grads, d_prev_params;

  for (size_t i = 0; i < params.size(); i++) {
    auto bps_param = std::make_shared<TorchTensor>(params[i]);
    d_params.push_back(std::move(bps_param));
    auto bps_grad = std::make_shared<TorchTensor>(grads[i]);
    d_grads.push_back(std::move(bps_grad));
    auto bps_prev_param = std::make_shared<TorchTensor>(prev_params[i]);
    d_prev_params.push_back(std::move(bps_prev_param));
  }

  auto curr_stream = c10::cuda::getCurrentCUDAStream();
  cudaStream_t curr_stream_t = curr_stream.stream();
  CompensateGrads(d_params, d_grads, d_prev_params, lambda, curr_stream_t);
  return 0;
}


int DCAdam(std::vector<::torch::Tensor> params, std::vector<::torch::Tensor> grads,
           std::vector<::torch::Tensor> prev_params, float dc_lambda,
           std::vector<::torch::Tensor> exp_avgs, std::vector<::torch::Tensor> exp_avg_sqs, std::vector<int64_t> steps,
           float lr, float eps, float weight_decay, float beta1, float beta2) {
             
  std::vector<std::shared_ptr<Tensor>> d_params, d_grads, d_prev_params;
  std::vector<std::shared_ptr<Tensor>> d_exp_avgs, d_exp_avg_sqs;

  for (size_t i = 0; i < params.size(); i++) {
    d_params.push_back(std::make_shared<TorchTensor>(params[i]));
    d_grads.push_back(std::make_shared<TorchTensor>(grads[i]));
    d_prev_params.push_back(std::make_shared<TorchTensor>(prev_params[i]));

    d_exp_avgs.push_back(std::make_shared<TorchTensor>(exp_avgs[i]));
    d_exp_avg_sqs.push_back(std::make_shared<TorchTensor>(exp_avg_sqs[i]));
  }

  auto curr_stream = c10::cuda::getCurrentCUDAStream();
  cudaStream_t curr_stream_t = curr_stream.stream();
  DCAdamLocalOp(d_params, d_grads, d_prev_params, dc_lambda,
                  d_exp_avgs, d_exp_avg_sqs, steps, lr, eps, weight_decay,
                  beta1, beta2, curr_stream_t);
  return 0;
}
#endif


PYBIND11_MODULE(c_lib, m) {
  // push_pull
  m.def("byteps_torch_push_pull_async_torch_ByteTensor", &DoPushPull);
  m.def("byteps_torch_push_pull_async_torch_IntTensor", &DoPushPull);
  m.def("byteps_torch_push_pull_async_torch_LongTensor", &DoPushPull);
  m.def("byteps_torch_push_pull_async_torch_HalfTensor", &DoPushPull);
  m.def("byteps_torch_push_pull_async_torch_FloatTensor", &DoPushPull);
  m.def("byteps_torch_push_pull_async_torch_DoubleTensor", &DoPushPull);

  m.def("byteps_torch_send_async_torch_BoolTensor", &DoSend);
  m.def("byteps_torch_send_async_torch_IntTensor", &DoSend);
  m.def("byteps_torch_send_async_torch_LongTensor", &DoSend);
  m.def("byteps_torch_send_async_torch_FloatTensor", &DoSend);
  m.def("byteps_torch_send_async_torch_DoubleTensor", &DoSend);

  m.def("byteps_torch_recv_async_torch_BoolTensor", &DoRecv);
  m.def("byteps_torch_recv_async_torch_IntTensor", &DoRecv);
  m.def("byteps_torch_recv_async_torch_LongTensor", &DoRecv);
  m.def("byteps_torch_recv_async_torch_FloatTensor", &DoRecv);
  m.def("byteps_torch_recv_async_torch_DoubleTensor", &DoRecv);

  m.def("byteps_torch_set_num_grads", &SetNumGrads);

  m.def("byteps_torch_push_pull_group_sync_torch_ByteTensor", &DoPushPullGroupSync);
  m.def("byteps_torch_push_pull_group_sync_torch_IntTensor", &DoPushPullGroupSync);
  m.def("byteps_torch_push_pull_group_sync_torch_LongTensor", &DoPushPullGroupSync);
  m.def("byteps_torch_push_pull_group_sync_torch_HalfTensor", &DoPushPullGroupSync);
  m.def("byteps_torch_push_pull_group_sync_torch_FloatTensor", &DoPushPullGroupSync);
  m.def("byteps_torch_push_pull_group_sync_torch_DoubleTensor", &DoPushPullGroupSync);

  m.def("byteps_torch_allgather_async_torch_ByteTensor", &DoAllgather);
  m.def("byteps_torch_allgather_async_torch_BoolTensor", &DoAllgather);
  m.def("byteps_torch_allgather_async_torch_IntTensor", &DoAllgather);
  m.def("byteps_torch_allgather_async_torch_LongTensor", &DoAllgather);
  m.def("byteps_torch_allgather_async_torch_HalfTensor", &DoAllgather);
  m.def("byteps_torch_allgather_async_torch_FloatTensor", &DoAllgather);
  m.def("byteps_torch_allgather_async_torch_DoubleTensor", &DoAllgather);

#if HAVE_CUDA
  m.def("byteps_torch_push_pull_async_torch_cuda_ByteTensor", &DoPushPull);
  m.def("byteps_torch_push_pull_async_torch_cuda_IntTensor", &DoPushPull);
  m.def("byteps_torch_push_pull_async_torch_cuda_LongTensor", &DoPushPull);
  m.def("byteps_torch_push_pull_async_torch_cuda_HalfTensor", &DoPushPull);
  m.def("byteps_torch_push_pull_async_torch_cuda_FloatTensor", &DoPushPull);
  m.def("byteps_torch_push_pull_async_torch_cuda_DoubleTensor", &DoPushPull);

  m.def("byteps_torch_send_async_torch_cuda_BoolTensor", &DoSend);
  m.def("byteps_torch_send_async_torch_cuda_IntTensor", &DoSend);
  m.def("byteps_torch_send_async_torch_cuda_LongTensor", &DoSend);
  m.def("byteps_torch_send_async_torch_cuda_HalfTensor", &DoSend);
  m.def("byteps_torch_send_async_torch_cuda_FloatTensor", &DoSend);
  m.def("byteps_torch_send_async_torch_cuda_DoubleTensor", &DoSend);

  m.def("byteps_torch_recv_async_torch_cuda_BoolTensor", &DoRecv);
  m.def("byteps_torch_recv_async_torch_cuda_IntTensor", &DoRecv);
  m.def("byteps_torch_recv_async_torch_cuda_LongTensor", &DoRecv);
  m.def("byteps_torch_recv_async_torch_cuda_HalfTensor", &DoRecv);
  m.def("byteps_torch_recv_async_torch_cuda_FloatTensor", &DoRecv);
  m.def("byteps_torch_recv_async_torch_cuda_DoubleTensor", &DoRecv);

  m.def("byteps_torch_push_pull_group_sync_torch_cuda_ByteTensor", &DoPushPullGroupSync);
  m.def("byteps_torch_push_pull_group_sync_torch_cuda_IntTensor", &DoPushPullGroupSync);
  m.def("byteps_torch_push_pull_group_sync_torch_cuda_LongTensor", &DoPushPullGroupSync);
  m.def("byteps_torch_push_pull_group_sync_torch_cuda_HalfTensor", &DoPushPullGroupSync);
  m.def("byteps_torch_push_pull_group_sync_torch_cuda_FloatTensor", &DoPushPullGroupSync);
  m.def("byteps_torch_push_pull_group_sync_torch_cuda_DoubleTensor", &DoPushPullGroupSync);

  m.def("byteps_torch_batched_fuse_async_torch_cuda_ByteTensor", &BatchedFuse);
  m.def("byteps_torch_batched_fuse_async_torch_cuda_IntTensor", &BatchedFuse);
  m.def("byteps_torch_batched_fuse_async_torch_cuda_LongTensor", &BatchedFuse);
  m.def("byteps_torch_batched_fuse_async_torch_cuda_HalfTensor", &BatchedFuse);
  m.def("byteps_torch_batched_fuse_async_torch_cuda_FloatTensor", &BatchedFuse);
  m.def("byteps_torch_batched_fuse_async_torch_cuda_DoubleTensor", &BatchedFuse);

  m.def("byteps_torch_batched_unfuse_async_torch_cuda_ByteTensor", &BatchedUnfuse);
  m.def("byteps_torch_batched_unfuse_async_torch_cuda_IntTensor", &BatchedUnfuse);
  m.def("byteps_torch_batched_unfuse_async_torch_cuda_LongTensor", &BatchedUnfuse);
  m.def("byteps_torch_batched_unfuse_async_torch_cuda_HalfTensor", &BatchedUnfuse);
  m.def("byteps_torch_batched_unfuse_async_torch_cuda_FloatTensor", &BatchedUnfuse);
  m.def("byteps_torch_batched_unfuse_async_torch_cuda_DoubleTensor", &BatchedUnfuse);

  m.def("byteps_torch_batched_zero_out_async_torch_cuda_ByteTensor", &BatchedZeroOut);
  m.def("byteps_torch_batched_zero_out_async_torch_cuda_IntTensor", &BatchedZeroOut);
  m.def("byteps_torch_batched_zero_out_async_torch_cuda_LongTensor", &BatchedZeroOut);
  m.def("byteps_torch_batched_zero_out_async_torch_cuda_HalfTensor", &BatchedZeroOut);
  m.def("byteps_torch_batched_zero_out_async_torch_cuda_FloatTensor", &BatchedZeroOut);
  m.def("byteps_torch_batched_zero_out_async_torch_cuda_DoubleTensor", &BatchedZeroOut);

  m.def("byteps_torch_delay_compensation_async_torch_cuda_ByteTensor", &DelayCompensation);
  m.def("byteps_torch_delay_compensation_async_torch_cuda_IntTensor", &DelayCompensation);
  m.def("byteps_torch_delay_compensation_async_torch_cuda_LongTensor", &DelayCompensation);
  m.def("byteps_torch_delay_compensation_async_torch_cuda_HalfTensor", &DelayCompensation);
  m.def("byteps_torch_delay_compensation_async_torch_cuda_FloatTensor", &DelayCompensation);
  m.def("byteps_torch_delay_compensation_async_torch_cuda_DoubleTensor", &DelayCompensation);

  m.def("byteps_torch_dc_adam_async_torch_cuda_ByteTensor", &DCAdam);
  m.def("byteps_torch_dc_adam_async_torch_cuda_IntTensor", &DCAdam);
  m.def("byteps_torch_dc_adam_async_torch_cuda_LongTensor", &DCAdam);
  m.def("byteps_torch_dc_adam_async_torch_cuda_HalfTensor", &DCAdam);
  m.def("byteps_torch_dc_adam_async_torch_cuda_FloatTensor", &DCAdam);
  m.def("byteps_torch_dc_adam_async_torch_cuda_DoubleTensor", &DCAdam);

  m.def("byteps_torch_allgather_async_torch_cuda_ByteTensor", &DoAllgather);
  m.def("byteps_torch_allgather_async_torch_cuda_BoolTensor", &DoAllgather);
  m.def("byteps_torch_allgather_async_torch_cuda_IntTensor", &DoAllgather);
  m.def("byteps_torch_allgather_async_torch_cuda_LongTensor", &DoAllgather);
  m.def("byteps_torch_allgather_async_torch_cuda_HalfTensor", &DoAllgather);
  m.def("byteps_torch_allgather_async_torch_cuda_FloatTensor", &DoAllgather);
  m.def("byteps_torch_allgather_async_torch_cuda_DoubleTensor", &DoAllgather);
  
  m.def("byteps_torch_delay_compensation_async_torch_cuda_ByteTensor", &DelayCompensation);
  m.def("byteps_torch_delay_compensation_async_torch_cuda_IntTensor", &DelayCompensation);
  m.def("byteps_torch_delay_compensation_async_torch_cuda_LongTensor", &DelayCompensation);
  m.def("byteps_torch_delay_compensation_async_torch_cuda_HalfTensor", &DelayCompensation);
  m.def("byteps_torch_delay_compensation_async_torch_cuda_FloatTensor", &DelayCompensation);
  m.def("byteps_torch_delay_compensation_async_torch_cuda_DoubleTensor", &DelayCompensation);
#endif

  // basics
  m.def("byteps_torch_poll", &PollHandle);
  m.def("byteps_torch_wait_and_clear", &WaitAndClear);
  m.def("byteps_torch_declare_tensor", &DeclareTensor);
  m.def("byteps_torch_declare_tensor_p2p", &DeclareTensorP2P);
  m.def("byteps_torch_declare_tensor_allgather", &DeclareTensorAllgather);
}

}  // namespace torch
}  // namespace byteps
