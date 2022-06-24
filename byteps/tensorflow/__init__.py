# Copyright 2019 Bytedance Inc. All Rights Reserved.
# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
# Modifications copyright (C) 2019 Uber Technologies, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
# pylint: disable=g-short-docstring-punctuation

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import warnings

from byteps.tensorflow.compression import Compression
from byteps.tensorflow.ops import broadcast, _push_pull
from byteps.tensorflow.ops import init, shutdown, is_initialized, suspend, resume, get_pushpull_speed
from byteps.tensorflow.ops import _alltoall, _alltoall_cpu2gpu, _alltoall_gpu2cpu, _allgather
from byteps.tensorflow.ops import send_async, recv_async
from byteps.tensorflow.ops import size, local_size, rank, local_rank
from byteps.tensorflow.ops import get_telemetry
from byteps.tensorflow.ops import handle_average_backwards_compatibility
from byteps.tensorflow.ops import Average, Sum, Adasum
from byteps.tensorflow.util import _executing_eagerly

import tensorflow as tf
from tensorflow.python.ops import control_flow_ops

def push_pull(tensor, scope='', average=None, device_dense='', device_sparse='',
              compression=Compression.none, op=None, enable_async=False,
              name=None):
    """Perform an push_pull on a tf.Tensor or tf.IndexedSlices.
    Arguments:
        tensor: tf.Tensor, tf.Variable, or tf.IndexedSlices to reduce.
                The shape of the input must be identical across all ranks.
        average:
            .. warning:: .. deprecated

                Use `op` instead. Will be removed.

        scope: the graph name scope
        average: If True, computes the average over all ranks.
                 Otherwise, computes the sum over all ranks.
        device_dense: Device to be used for dense tensors. Uses GPU by default.
        device_sparse: Device to be used for sparse tensors. Uses GPU by default.
        compression: Compression algorithm used to reduce the amount of data
                     sent and received by each worker node.  Defaults to not
                     using compression.
        op: The reduction operation to combine tensors across different ranks.
            Defaults to Average if None is given.

    Returns:
        A tensor of the same shape and type as `tensor`, summed across all
        processes.
    """
    op = handle_average_backwards_compatibility(op, average)

    with tf.device(device_dense):
        byteps_size = tf.cast(size(), dtype=tensor.dtype)
        tensor_compressed, ctx = compression.compress(tensor)
        reduced_tensor_compressed = _push_pull(tensor_compressed, scope, name, op)
        reduced_tensor = compression.decompress(reduced_tensor_compressed, ctx)
        if enable_async: # no need to average for async training
            new_tensor = reduced_tensor
        else:
            # c++ gradient compressors does not support performing average on
            # the server side. For MXNet and PyTorch, we need to check if a
            # particular tensor has registered c++ compressor. If so, we need to
            # disable server-side averaging.
            if op == Average and size() == local_size() \
                   and os.getenv("BYTEPS_FORCE_DISTRIBUTED", "0") != "1":
                _div = tf.div if hasattr(tf, 'div') else tf.math.divide
                new_tensor = _div(reduced_tensor, byteps_size)
            else:
                new_tensor = reduced_tensor
    return new_tensor


def alltoall(tensor, splits, recv_splits=None, scope='', name=None,
             with_size=False, compression=Compression.none):
    """An op that scatters slices of the input tensor(s) to all other BytePS processes
    and returns the gathered slices from all other BytePS processes.
    The slicing is done on the first dimension, so the input tensors on the
    different processes must have the same rank and shape, except for the first
    dimension, which is allowed to be different.

    Arguments:
        tensor: A tensor or a list/tuple of tensors to distribute with alltoall.
        splits: A tensor of integers in rank order describing how many
                elements in `tensor` to send to each worker.  Splitting is
                applied along the first dimension of `tensor`.
        recv_splits: A tensor of integers in rank order describing how many
                elements in `tensor` to receive from each worker.  Splitting is
                applied along the first dimension of other ranks' `tensor`.
        name: A name of the alltoall operation.
        with_size: return the `recv_splits`

    Returns:
      If the input `tensor` is a single tensor, then returns a tensor of 
      the same type as `tensor`, concatenated on dimension zero across all processes. 
      If the input `tensor` is a list/tuple of tensors, then returns a list of tensors
      with the size and type as `tensor`, with their size specified by `recv_splits`.
      For each tensor, The shape is identical to the input shape, except for
      the first dimension, which may be greater and is the sum of all first
      dimensions of the gathered tensor slices from different BytePS processes.
      If with_size is True, return the received splits.
    """
    results = _alltoall(tensor, scope, splits=splits, recv_splits=recv_splits,
                        name=name, with_size=with_size, compression=compression)
    return results


def alltoall_cpu2gpu(tensor, splits, recv_splits=None, scope='', name=None,
                     with_size=False, compression=Compression.none):
    """An op that scatters slices of the input tensor to all other BytePS processes
    and returns a tensor of gathered slices from all other BytePS processes.
    The slicing is done on the first dimension, so the input tensors on the
    different processes must have the same rank and shape, except for the first
    dimension, which is allowed to be different. Different from `alltoall`, this
    operator sends tensor on CPUs to remote GPUs.

    Arguments:
        tensor: A tensor to distribute with alltoall on CPU
        splits: A tensor of integers in rank order describing how many
                elements in `tensor` to send to each worker.  Splitting is
                applied along the first dimension of `tensor` on CPU
        recv_splits: A tensor of integers in rank order describing how many
                elements in `tensor` to receive from each worker.  Splitting is
                applied along the first dimension of other ranks' `tensor` on CPU
        name: A name of the alltoall operation.
        with_size: return the `recv_splits`

    Returns:
      A tensor of the same type as `tensor` on GPU, concatenated on dimension zero
      across all processes. The shape is identical to the input shape, except for
      the first dimension, which may be greater and is the sum of all first
      dimensions of the gathered tensor slices from different BytePS processes.
      If with_size is True, return the received splits.
    """
    results = _alltoall_cpu2gpu(tensor, scope, splits=splits, recv_splits=recv_splits,
                                name=name, with_size=with_size, compression=compression)
    return results

def alltoall_gpu2cpu(tensor, splits, recv_splits=None, scope='', name=None,
                     with_size=False, compression=Compression.none):
    """An op that scatters slices of the input tensor to all other BytePS processes
    and returns a tensor of gathered slices from all other BytePS processes.
    The slicing is done on the first dimension, so the input tensors on the
    different processes must have the same rank and shape, except for the first
    dimension, which is allowed to be different. Different from `alltoall`, this
    operator sends tensor on GPUs to remote CPUs.

    Arguments:
        tensor: A tensor to distribute with alltoall on GPU
        splits: A tensor of integers in rank order describing how many
                elements in `tensor` to send to each worker.  Splitting is
                applied along the first dimension of `tensor` on GPU
        recv_splits: A tensor of integers in rank order describing how many
                elements in `tensor` to receive from each worker.  Splitting is
                applied along the first dimension of other ranks' `tensor` on GPU
        name: A name of the alltoall operation.
        with_size: return the `recv_splits`

    Returns:
      A tensor of the same type as `tensor` on CPU, concatenated on dimension zero
      across all processes. The shape is identical to the input shape, except for
      the first dimension, which may be greater and is the sum of all first
      dimensions of the gathered tensor slices from different BytePS processes.
      If with_size is True, return the received splits.
    """
    results = _alltoall_gpu2cpu(tensor, scope, splits=splits, recv_splits=recv_splits,
                                name=name, with_size=with_size, compression=compression)
    return results


try:
    _global_variables = tf.global_variables
except AttributeError:
    try:
        _global_variables = tf.compat.v1.global_variables
    except AttributeError:
        _global_variables = None

if _global_variables is not None:
    def broadcast_global_variables(root_rank):
        """Broadcasts all global variables from root rank to all other processes.

        **NOTE:** deprecated in TensorFlow 2.0.

        Arguments:
            root_rank: rank of the process from which global variables will be broadcasted
                       to all other processes.
        """
        if _executing_eagerly():
            raise RuntimeError(
                "bps.broadcast_global_variables() does not support eager execution. "
                "Please use `bps.broadcast_variables(<model/optimizer variables>)` instead."
            )

        return broadcast_variables(_global_variables(), root_rank)

def allgather(tensor, same_shape=True, scope='', name=None):
    """An op which concatenates the input tensor with the same input tensor on
    all other BytePS processes.
    Arguments:
        tensor: A tensor to distribute with allgather.
        same_shape: Whether the tensor is with the same shape over all ranks or not.
                    If true, the performance will be better, but user should make sure
                    all tensors are with the same shape.
        scope: the graph name scope
        name: A name of the allgather operation.

    Returns:
        A tensor of the same type as `tensor`, concatenated on dimension zero
        across all processes. The shape is identical to the input shape, except for
        the first dimension, which may be greater and is the sum of all first
        dimensions of the tensors in different allgather processes.
    """
    results = _allgather(tensor, same_shape, scope, name=name)
    return results

def broadcast_variables(variables, root_rank, scope=''):
    """Broadcasts variables from root rank to all other processes.
    Arguments:
        variables: variables for broadcast
        root_rank: rank of the process from which global variables will be broadcasted
                   to all other processes.
        scope: the graph name scope
    """
    _assign = tf.assign if hasattr(tf, 'assign') else tf.compat.v1.assign
    return tf.group(*[_assign(var, broadcast(var, root_rank, scope))
                      for var in variables])

try:
    _get_default_graph = tf.get_default_graph
except AttributeError:
    try:
        _get_default_graph = tf.compat.v1.get_default_graph
    except AttributeError:
        _get_default_graph = None

try:
    _SessionRunHook = tf.estimator.SessionRunHook
except AttributeError:
    try:
        _SessionRunHook = tf.train.SessionRunHook
    except AttributeError:
        _SessionRunHook = None

if _SessionRunHook is not None and _get_default_graph is not None:
    class BroadcastGlobalVariablesHook(_SessionRunHook):
        """
        SessionRunHook that will broadcast all global variables from root rank
        to all other processes during initialization.

        This is necessary to ensure consistent initialization of all workers when
        training is started with random weights or restored from a checkpoint.

        **NOTE:** deprecated in TensorFlow 2.0.
        """

        def __init__(self, root_rank, device=''):
            """Construct a new BroadcastGlobalVariablesHook that will broadcast all
            global variables from root rank to all other processes during initialization.

            Args:
              root_rank:
                Rank that will send data, other ranks will receive data.
              device:
                Device to be used for broadcasting. Uses GPU by default.
            """
            super(BroadcastGlobalVariablesHook, self).__init__()
            self.root_rank = root_rank
            self.bcast_op = None
            self.device = device

        def begin(self):
            if not self.bcast_op or self.bcast_op.graph != _get_default_graph():
                with tf.device(self.device):
                    self.bcast_op = broadcast_global_variables(self.root_rank)

        def after_create_session(self, session, coord):
            session.run(self.bcast_op)

try:
    # TensorFlow 2.x
    _LegacyOptimizer = tf.compat.v1.train.Optimizer
except AttributeError:
    try:
        # TensorFlow 1.x
        _LegacyOptimizer = tf.train.Optimizer
    except AttributeError:
        # Future TensorFlow versions
        _LegacyOptimizer = None

if _LegacyOptimizer is not None:
    class _DistributedOptimizer(_LegacyOptimizer):
        """An optimizer that wraps another tf.Optimizer, using an push_pull to
        average gradient values before applying gradients to model weights."""

        def __init__(self, optimizer, name=None, use_locking=False, device_dense='',
                    device_sparse='', compression=Compression.none,
                    sparse_as_dense=False, op=Average):
            if name is None:
                name = "Distributed{}".format(type(optimizer).__name__)
            super(_DistributedOptimizer, self).__init__(name=name, use_locking=use_locking)

            self._optimizer = optimizer
            self._device_dense = device_dense
            self._device_sparse = device_sparse
            self._compression = compression
            self._sparse_as_dense = sparse_as_dense

            self._enable_async = (int(os.getenv('BYTEPS_ENABLE_ASYNC', 0)) != 0)
            if self._enable_async:
                assert int(os.getenv('DMLC_NUM_WORKER')) > 1, \
                    "Async is only valid for distributed training"
                print('BytePS: enable asynchronous training')

            def push_pull_grads(grads):
                with tf.name_scope(self._name + "_Push_Pull") as scope:
                    if self._sparse_as_dense:
                        grads = [tf.convert_to_tensor(grad)
                                if grad is not None and isinstance(grad, tf.IndexedSlices)
                                else grad for grad in grads]

                    return [push_pull(grad, scope,
                                    device_dense=self._device_dense,
                                    device_sparse=self._device_sparse,
                                    compression=self._compression,
                                    enable_async=self._enable_async)
                            if grad is not None else grad
                            for grad in grads]

            if _executing_eagerly():
                self._push_pull_grads = tf.contrib.eager.defun(push_pull_grads)
            else:
                self._push_pull_grads = push_pull_grads

        def compute_gradients(self, *args, **kwargs):
            """Compute gradients of all trainable variables.
            See Optimizer.compute_gradients() for more info.
            In DistributedOptimizer, compute_gradients() is overriden to also
            push_pull the gradients before returning them.
            """
            gradients = self._optimizer.compute_gradients(*args, **kwargs)
            if size() > 1 and not self._enable_async:
                grads, vars = zip(*gradients)
                avg_grads = self._push_pull_grads(grads)
                return list(zip(avg_grads, vars))
            else:
                return gradients

        def apply_gradients(self, *args, **kwargs):
            """Calls this same method on the underlying optimizer."""
            if self._enable_async: # async training
                grads_and_vars = args[0]
                _, vars = zip(*grads_and_vars)
                old_tensors = []
                for var in vars:
                    old_tensors.append(tf.convert_to_tensor(var))
                apply_ops = self._optimizer.apply_gradients(*args, **kwargs)
                with tf.control_dependencies([apply_ops]):
                    # get the delta
                    for i, var in enumerate(vars):
                        old_tensors[i] = tf.subtract(var, old_tensors[i])

                    # reuse the _push_pul_grads(), but is transferring parameters
                    updated_tensors = self._push_pull_grads(old_tensors)

                    # copy the updated variable back
                    assign_op_list = []
                    for i, tensor in enumerate(updated_tensors):
                        assign_op_list.append(tf.assign(vars[i], tensor))

                return control_flow_ops.group(*assign_op_list)
            else:
                return self._optimizer.apply_gradients(*args, **kwargs)

        def get_slot(self, *args, **kwargs):
            """Calls this same method on the underlying optimizer."""
            return self._optimizer.get_slot(*args, **kwargs)

        def get_slot_names(self, *args, **kwargs):
            """Calls this same method on the underlying optimizer."""
            return self._optimizer.get_slot_names(*args, **kwargs)

        def variables(self, *args, **kwargs):
            """Calls this same method on the underlying optimizer."""
            return self._optimizer.variables(*args, **kwargs)

def DistributedOptimizer(optimizer, name=None, use_locking=False, device_dense='',
                         device_sparse='', compression=Compression.none,
                         sparse_as_dense=False, backward_passes_per_step=1,
                         op=Average):
    """Construct a new DistributedOptimizer, which uses another optimizer
    under the hood for computing single-process gradient values and
    applying gradient updates after the gradient values have been combined
    across all the BytePS ranks.

    Args:
      optimizer:
        Optimizer to use for computing gradients and applying updates.
      name:
        Optional name prefix for the operations created when applying
        gradients. Defaults to "Distributed" followed by the provided
        optimizer type.
      use_locking:
        Whether to use locking when updating variables.
        See Optimizer.__init__ for more info.
      device_dense:
        Device to be used for dense tensors. Uses GPU by default.
      device_sparse:
        Device to be used for sparse tensors. Uses GPU by default.
      compression:
        Compression algorithm used during push_pull to reduce the amount
        of data sent during each parameter update step.  Defaults to
        not using compression.
      sparse_as_dense:
        Treat all sparse gradients as dense tensors.  This can help improve
        performance and memory utilization if the original sparse gradient
        has high density.  Defaults to false.
      backward_passes_per_step:
        Number of backward passes to perform before calling bps.push_pull
        This allows accumulating updates over multiple mini-batches before
        reducing and applying them.
      op:
        The reduction operation to use when combining gradients across
        different ranks.
    """
    if isinstance(optimizer, _LegacyOptimizer):
        if op == Adasum:
            raise ValueError('op == Adasum is not supported yet with ')
        else:
            if backward_passes_per_step > 1:
                raise ValueError('backward_passes_per_step>1 is not supported yet with '
                                 'op != Adasum')
            return _DistributedOptimizer(optimizer, name, use_locking, device_dense,
                                        device_sparse, compression, sparse_as_dense, op)
    elif isinstance(optimizer, tf.keras.optimizers.Optimizer):
        if op == Adasum:
            raise ValueError('op == Adasum is not supported yet with Keras')
        if backward_passes_per_step > 1:
            raise ValueError('backward_passes_per_step > 1 is not supported yet with Keras')
        import byteps.tensorflow.keras as bps_k
        return bps_k.DistributedOptimizer(optimizer, name, device_dense, device_sparse,
                                          compression, sparse_as_dense)
    else:
        raise ValueError('Provided optimizer doesn\'t inherit from either legacy '
                         'TensorFlow or Keras optimizer: %s' % optimizer)


if hasattr(tf, 'GradientTape'):
    class _DistributedGradientTape(tf.GradientTape):
        def __init__(self, tape, device_dense, device_sparse, compression, sparse_as_dense, op,
                     persistent=False, watch_accessed_variables=True, fuse_common_names=[]):
            if hasattr(tape, '_watch_accessed_variables'):
                super(self.__class__, self).__init__(persistent, watch_accessed_variables)
            else:
                super(self.__class__, self).__init__(persistent)

            self._tape = tape
            self._persistent = persistent
            self._watch_accessed_variables = watch_accessed_variables
            self._name = "Distributed"
            self._device_dense = device_dense
            self._device_sparse = device_sparse
            self._compression = compression
            self._sparse_as_dense = sparse_as_dense
            self._fuse_common_names = fuse_common_names

            def _group_push_pull_grads(grads, scope, device_dense, device_sparse, compression, common_name=''):
                grads_wo_none = [grad for grad in grads if (grad is not None) and (None not in grad.shape)]
                if len(grads_wo_none) == 0: return grads
                if len(grads_wo_none) == 1: 
                    return [push_pull(grad, scope, device_dense=self._device_dense,
                                            device_sparse=self._device_sparse, compression=self._compression)
                                if grad is not None else grad for grad in grads]
                reshaped_grads_fp32, grad_shapes_fp32, grad_lens_fp32 = [], [], []
                reshaped_grads_fp16, grad_shapes_fp16, grad_lens_fp16 = [], [], []
                # reshape to 1D tensor
                for idx in range(len(grads_wo_none)):
                    grad = grads_wo_none[idx]
                    reshaped_grad = tf.reshape(grad, [-1]) if len(grad.shape) != 1 else grad
                    grads_fused_fp32, grads_fused_fp16 = None, None
                    if grad.dtype == tf.float32:
                        grad_shapes_fp32.append(grad.shape)
                        grad_lens_fp32.append(tf.size(reshaped_grad))
                        reshaped_grads_fp32.append(reshaped_grad)
                    else:
                        grad_shapes_fp16.append(grad.shape)
                        grad_lens_fp16.append(reshaped_grad.shape[0])
                        reshaped_grads_fp16.append(reshaped_grad)
                if len(reshaped_grads_fp32) > 0: 
                    grads_fused_fp32 = tf.concat(reshaped_grads_fp32, axis=0, name='concat_allreduce_fp32')
                    grads_fused_fp32_avg = push_pull(grads_fused_fp32, name=common_name+"_fused_fp32", 
                                                        scope=scope, device_dense=self._device_dense,
                                                        device_sparse=self._device_sparse, 
                                                        compression=self._compression) 
                    grads_fp32_avg_split = tf.split(grads_fused_fp32_avg, grad_lens_fp32, axis=0)
                if len(reshaped_grads_fp16)> 0: 
                    grads_fused_fp16 = tf.concat(reshaped_grads_fp16, axis=0, name='concat_allreduce_fp16')
                    grads_fused_fp16_avg = push_pull(grads_fused_fp16, name=common_name+"_fused_fp16",
                                                        scope=scope, device_dense=self._device_dense,
                                                        device_sparse=self._device_sparse, 
                                                        compression=self._compression) 
                    grads_fp16_avg_split = tf.split(grads_fused_fp16_avg, grad_lens_fp16, axis=0)
                # now split the tensors according to their initial shape
                num_grads = len(grads)
                results = [None for _ in range(num_grads)]
                i_32, i_16 = 0, 0
                for idx in range(num_grads):
                    grad = grads[idx]
                    if (grad is not None):
                        if None in grad.shape:
                            results[idx] = push_pull(grad, scope=scope, name=common_name+"_unknown_shape_tensor_" + str(idx),
                                                        device_dense=self._device_dense,
                                                        device_sparse=self._device_sparse, 
                                                        compression=self._compression)
                        else:
                            if len(reshaped_grads_fp32) > 0 and grad.dtype == tf.float32:
                                results[idx] = tf.reshape(grads_fp32_avg_split[i_32], grad_shapes_fp32[i_32]) \
                                                if len(grad_shapes_fp32[i_32]) != 1 else grads_fp32_avg_split[i_32]
                                i_32 += 1
                            elif len(reshaped_grads_fp16) > 0 and grad.dtype == tf.float16:
                                results[idx] = tf.reshape(grads_fp16_avg_split[i_16], grad_shapes_fp16[i_16]) \
                                                if len(grad_shapes_fp16[i_16]) != 1 else grads_fp16_avg_split[i_16]
                                i_16 += 1
                return results

            def push_pull_grads(grads):
                with tf.name_scope(self._name + "_Push_Pull") as scope:
                    if self._sparse_as_dense:
                        grads = [tf.convert_to_tensor(grad)
                                 if grad is not None and isinstance(grad, tf.IndexedSlices)
                                 else grad for grad in grads]
                    if os.getenv("BYTEPS_TF_GRADIENT_FUSION", "0") == "1":
                        print("BytePS: Enable gradient fusion for TensorFlow.")
                        if len(self._fuse_common_names) == 0:
                            return _group_push_pull_grads(grads, scope, device_dense=self._device_dense,
                                                          device_sparse=self._device_sparse, compression=self._compression)
                        else:
                            assert all(isinstance(x, str) for x in self._fuse_common_names) \
                                    or all(isinstance(x, list) for x in self._fuse_common_names)                            
                            # prepare non-empty grads
                            all_grads = list(grads)
                            grads = []
                            not_none_idxes = []
                            for i, grad in enumerate(all_grads):
                                if grad is not None: 
                                    assert hasattr(grad, 'name')
                                    grads.append(grad)
                                    not_none_idxes.append(i)
                            # hierarchical 
                            is_hierarchical_group = isinstance(self._fuse_common_names[0], list) 
                            if is_hierarchical_group:
                                num_group = len(self._fuse_common_names)
                                prefix = 'fuse_group_'
                                groups = {(prefix+str(i)):[] for i in range(num_group)}
                                name_to_id = {grad.name: i for i, grad in enumerate(grads)}
                                for grad in grads:
                                    found = False
                                    for i, names in enumerate(self._fuse_common_names):
                                        if found: break
                                        for name in names:
                                            if name in grad.name:
                                                groups[prefix + str(i)].append(grad)
                                                found = True
                                                break
                                    if not found: 
                                        groups[grad.name] = [grad]
                            else:
                                groups = {name:[] for name in self._fuse_common_names}
                                name_to_id = {grad.name: i for i, grad in enumerate(grads)}
                                for grad in grads:
                                    found = False
                                    for name in self._fuse_common_names:
                                        if name in grad.name:
                                            groups[name].append(grad)
                                            found = True
                                            break
                                    if not found: 
                                        groups[grad.name] = [grad]
                            if local_rank() == 0:
                                print('==== BytePS Fusion Group Log (S) ====')
                                for common_name in groups:
                                    grad_names = [grad.name for grad in groups[common_name]]
                                    print(common_name + ': ', grad_names)
                                print('==== BytePS Fusion Group Log (E) ====')
                            each_group_res = {}
                            for common_name in groups:
                                grad_list = groups[common_name]
                                if len(groups[common_name]) == 0: continue
                                group_res = _group_push_pull_grads(grad_list, scope, device_dense=self._device_dense,
                                                        device_sparse=self._device_sparse, compression=self._compression, 
                                                        common_name=common_name)
                                assert len(grad_list) == len(group_res)
                                for i in range(len(grad_list)):
                                    grad_id = name_to_id[grad_list[i].name]
                                    each_group_res[grad_id] = group_res[i]
                            assert len(each_group_res) == len(name_to_id)
                            res = [None for _ in all_grads]
                            for i in range(len(name_to_id)):
                                res[not_none_idxes[i]] = each_group_res[i]
                            return res
                    else:
                        return [push_pull(grad, scope, device_dense=self._device_dense,
                                            device_sparse=self._device_sparse, compression=self._compression)
                                if grad is not None else grad
                                for grad in grads]
            self._push_pull_grads = push_pull_grads

        def gradient(self, target, sources, output_gradients=None):
            gradients = super(self.__class__, self).gradient(target, sources, output_gradients)
            if size() > 1:
                avg_grads = self._push_pull_grads(gradients)
                return avg_grads
            else:
                return gradients


    def DistributedGradientTape(gradtape, device_dense='', device_sparse='',
                                compression=Compression.none, sparse_as_dense=False,
                                op=Average, fuse_common_names=[]):
        """An tape that wraps another tf.GradientTape, using an push_pull to
        average gradient values before applying gradients to model weights.
        Args:
          gradtape:
            GradientTape to use for computing gradients and applying updates.
          device_dense:
            Device to be used for dense tensors. Uses GPU by default.
          device_sparse:
            Device to be used for sparse tensors. Uses GPU by default.
          compression:
            Compression algorithm used during push_pull to reduce the amount
            of data sent during the each parameter update step.  Defaults to
            not using compression.
          sparse_as_dense:
            Treat all sparse gradients as dense tensors.  This can help improve
            performance and memory utilization if the original sparse gradient
            has high density.  Defaults to false.
          op:
            The reduction operation to use when combining gradients across
            different ranks.
        """
        cls = type(gradtape.__class__.__name__, (gradtape.__class__,),
                   dict(_DistributedGradientTape.__dict__))
        if hasattr(gradtape, '_watch_accessed_variables'):
            return cls(gradtape._tape, device_dense, device_sparse, compression,
                       sparse_as_dense, op, gradtape._persistent,
                       gradtape._watch_accessed_variables, fuse_common_names)
        else:
            return cls(gradtape._tape, device_dense, device_sparse, compression,
                       sparse_as_dense, op, gradtape._persistent, fuse_common_names)
