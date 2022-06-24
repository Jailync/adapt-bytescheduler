#!/usr/bin/python3

from __future__ import print_function
import os
import re
import subprocess
import threading
import sys
import time
from functools import reduce


class PropagatingThread(threading.Thread):
    """ propagate exceptions to the parent's thread
    refer to https://stackoverflow.com/a/31614591/9601110
    """
    def __init__(self, callback=None, idx=-1, **kwargs):
        super().__init__(**kwargs)
        self.callback = callback
        self.idx = idx

    def run(self):
        self.exc = None
        try:
            if hasattr(self, '_Thread__target'):
                #  python 2.x
                self.ret = self._Thread__target(
                    *self._Thread__args, **self._Thread__kwargs)
            else:
                # python 3.x
                self.ret = self._target(*self._args, **self._kwargs)
        except BaseException as e:
            self.exc = e
        if self.callback is not None:
            self.callback(self.idx)

    def join(self):
        super(PropagatingThread, self).join()
        if self.exc:
            raise self.exc
        return self.exc


COMMON_REQUIRED_ENVS = ["BYTEPS_NUM_NODES", "BYTEPS_LOCAL_SIZE", "DMLC_ROLE",
                        "DMLC_PS_ROOT_URI", "DMLC_PS_ROOT_PORT"]
WORKER_REQUIRED_ENVS = ["BYTEPS_NODE_ID"]
NUMA_PATH = "/sys/devices/system/node"

def allocate_cpu(local_size):
    cpu_mt = os.getenv("BYTEPS_MULTITHREADED_CPU", "1").lower() in ["1", "true"]
    def get_numa_info():
        """
        returns a list of list, each sub list is the cpu ids of a numa node. e.g
        [[0,1,2,3], [4,5,6,7]]
        """
        ret = []
        if os.path.exists(NUMA_PATH):
            items = os.listdir(NUMA_PATH)
            nodes = list(filter(lambda str: str.startswith("node"), items))
            if nodes:
                for node in nodes:
                    items = os.listdir(os.path.join(NUMA_PATH, node))
                    cpus = [re.findall("cpu\d+", cpu) for cpu in items]
                    cpus = list(filter(lambda x: x, cpus))
                    cpu_ids = [int(cpu[0].split('cpu')[1]) for cpu in cpus]
                    cpu_ids = sorted(cpu_ids)
                    if cpu_mt:
                        cpu_ids = cpu_ids[:len(cpu_ids) // 2]
                    ret.append(cpu_ids)
        else:
            print("NUMA PATH %s NOT FOUND" % NUMA_PATH)
        return ret

    def _get_allocation(nodes, quota, cpu_num, cpu_blacklist):
        if quota < 1:
            raise ValueError("quota should be no less than 1")
        ret = []
        for node in nodes:
            if len(node) < quota:
                continue
            split_index = []
            for i in range(1, quota):
                if node[i] != node[i-1] + 1:
                    split_index.append(i)
            quota_bck = quota
            last_idx = 0
            for idx in split_index:
                ret.append(node[last_idx:idx])
                quota -= idx - last_idx
                last_idx = idx
            curr_alloc = node[last_idx:last_idx+quota]
            curr_alloc = [item for item in curr_alloc if item not in cpu_blacklist]
            ret.append(curr_alloc)
            if cpu_mt:
                curr_alloc = [x + cpu_num for x in curr_alloc]
                curr_alloc = [item for item in curr_alloc if item not in cpu_blacklist]
                ret.append(curr_alloc)
            for idx in sorted(range(quota_bck), reverse=True):
                del node[idx]
            return ret
        return ret

    def _get_quota(nodes, local_size):

        # default quota is the number of physical cores for non-root processess
        default_quota = cpu_num // local_size
        default_quota = int(os.getenv("BYTEPS_NUMA_DEFAULT_QUOTA", default_quota))
        while default_quota >= 1 and default_quota * local_size > cpu_num:
            default_quota -= 1

        # root quota is the number of cpus for root processess
        # root does more work, thus using more cpus
        root_quota = cpu_num - default_quota * (local_size - 1)
        if int(os.getenv("BYTEPS_NUMA_ROOT_QUOTA", 0)):
            root_quota = int(os.getenv("BYTEPS_NUMA_ROOT_QUOTA", 0))

        node_size = len(nodes[0])
        if cpu_mt:
            node_size //= 2
        while root_quota >= 1 and root_quota > node_size:
            root_quota -= 1
        return [default_quota] * (local_size - 1) + [root_quota]

    nodes = get_numa_info()
    if not nodes:
        return None
    cpu_num = reduce(lambda x, y: (x + len(y)), nodes, 0)
    quota_list = _get_quota(nodes, local_size)
    cpu_blacklist = os.getenv("BYTEPS_CPU_BLACKLIST", "-1")
    cpu_blacklist = [int(item) for item in cpu_blacklist.split(",")]
    ret = []
    for quota in quota_list:
        while quota > 0:
            allocation = _get_allocation(nodes, quota, cpu_num, cpu_blacklist)
            if allocation:
                ret.append(allocation)
                break
            else:
                quota -= 1

    return ret


def check_env():
    assert "DMLC_ROLE" in os.environ and \
           os.environ["DMLC_ROLE"].lower() in ["worker", "server", "scheduler", "joint"]
    required_envs = COMMON_REQUIRED_ENVS
    if os.environ["DMLC_ROLE"] in ["worker", "joint"]:
        assert "DMLC_NUM_WORKER" in os.environ
        num_worker = int(os.environ["DMLC_NUM_WORKER"])
        assert num_worker >= 1
        if num_worker == 1:
            required_envs = []
        required_envs += WORKER_REQUIRED_ENVS
    for env in required_envs:
        if env not in os.environ:
            print("The env " + env + " is missing")
            os._exit(-1)

def get_ucx_src_addr(local_size=1):
    ucx_rdmacm_src_addr = os.getenv("UCX_RDMA_CM_SOURCE_ADDRESS", "").split(',')
    if len(ucx_rdmacm_src_addr) == 1:
        ucx_rdmacm_src_addr *= local_size
    return ucx_rdmacm_src_addr

def is_joint_mode():
    return os.getenv("BYTEPS_FORCE_JOINT_MODE", "0").lower() in ["1", "true"]

def worker_fn(local_rank, local_size, command, allocation=None):
    my_env = os.environ.copy()
    my_env["BYTEPS_LOCAL_RANK"] = str(local_rank)
    my_env["BYTEPS_LOCAL_SIZE"] = str(local_size)
    ucx_src_addr = get_ucx_src_addr(local_size)
    if ucx_src_addr:
        my_env["UCX_RDMA_CM_SOURCE_ADDRESS"] = ucx_src_addr[local_rank]

    if int(os.getenv("BYTEPS_ENABLE_GDB", 0)):
        if command.find("python") != 0:
            command = "python " + command
        command = "gdb -ex 'run' -ex 'bt' -batch --args " + command

    if allocation:
        print("enable NUMA finetune...")
        retval = subprocess.call(
            ["dpkg", "-s", "numactl"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        if retval == 0:
            numa = "numactl --physcpubind "
            for cpu_set in allocation:
                if len(cpu_set) == 1:
                    numa += "{},".format(cpu_set[0])
                else:
                    numa += "{}-{},".format(cpu_set[0], cpu_set[-1])
            numa = numa.strip(',') + ' '
            command = numa + command
            print("Command: %s\n" % command)
        else:
            print("Warning: numactl not found. try `sudo apt-get install numactl`.")
    if os.environ.get("BYTEPS_TRACE_ON", "") == "1":
        print("\n!!!Enable profiling for WORKER_ID: %s and local_rank: %d!!!" %
              (os.environ.get("DMLC_WORKER_ID"), local_rank))
        print("BYTEPS_TRACE_START_STEP: %s\tBYTEPS_TRACE_END_STEP: %s\t BYTEPS_TRACE_DIR: %s" % (os.environ.get(
            "BYTEPS_TRACE_START_STEP", ""), os.environ.get("BYTEPS_TRACE_END_STEP", ""), os.environ.get("BYTEPS_TRACE_DIR", "")))
        print("Command: %s\n" % command)
        sys.stdout.flush()
        trace_path = os.path.join(os.environ.get(
            "BYTEPS_TRACE_DIR", "."), str(local_rank))
        if not os.path.exists(trace_path):
            os.makedirs(trace_path)
    node_id = int(os.environ.get("BYTEPS_NODE_ID", "0"))
    global_rank = int(node_id * local_size + local_rank)
    if os.environ.get("DMLC_ROLE") == "joint":
        dmlc_worker_id = int(node_id * local_size + local_rank)
    else:
        dmlc_worker_id = int(node_id)
    my_env["DMLC_WORKER_ID"] = str(dmlc_worker_id)
    my_env["DMLC_RANK"] = my_env["DMLC_WORKER_ID"]
    log_file_name = os.getenv('BYTEPS_LOG_FILE', '')
    if log_file_name:
        stdout_sink = open(f"{log_file_name}-g{global_rank}-l{local_rank}-stdout.log", "w+")
        stderr_sink = open(f"{log_file_name}-g{global_rank}-l{local_rank}-stderr.log", "w+")
    else:
        stdout_sink = sys.stdout
        stderr_sink = sys.stderr

    subprocess.check_call(command, env=my_env,
                          stdout=stdout_sink, stderr=stderr_sink, shell=True)

def server_fn(local_rank, local_size, command, allocation=None):
    my_env = os.environ.copy()
    my_env["BYTEPS_LOCAL_RANK"] = str(local_rank)
    if int(os.getenv("BYTEPS_ENABLE_GDB", 0)):
        command = "gdb -ex 'run' -ex 'bt' -batch --args " + command
    my_env["DMLC_RANK"] = "-1"
    ucx_src_addr = get_ucx_src_addr(local_size)
    if ucx_src_addr:
        my_env["UCX_RDMA_CM_SOURCE_ADDRESS"] = ucx_src_addr[local_rank]

    subprocess.check_call(command, env=my_env,
                          stdout=sys.stdout, stderr=sys.stderr, shell=True)

def parse_num_range(core_list):
    # core_list is a colon-seperated string. each section is the physical
    # core assignment for the corresponding byteps worker.
    # example input: 1,4-5,7-11,12:20-25
    # example output: [[[1], [4, 5], [7, 8, 9, 10, 11], [12]], [[20, 21, 22, 23, 24, 25]]]
    core_list = core_list.split(':')
    ret = []
    for item in core_list:
        temp = [(lambda sub: range(sub[0], sub[-1] + 1))(list(map(int, elem.split('-')))) for elem in item.split(',')]
        ret.append([list(a) for a in temp])
    return ret

cv = threading.Condition(lock=threading.Lock())
done_threads = []

def done_callback(idx):
    with cv:
        done_threads.append(idx)
        cv.notify()

def join_threads(threads):
    count = 0
    num = len(threads)
    while count < num:
        with cv:
            while not done_threads:
                cv.wait()
            idx = done_threads[-1]
            done_threads.pop()
        threads[idx].join()
        print("BytePS launcher: joined local rank ", idx)
        count += 1

def launch_bps():
    print("BytePS launching " + os.environ["DMLC_ROLE"])
    sys.stdout.flush()
    check_env()
    os.environ["PYTHONUNBUFFERED"] = "1"
    if os.environ["DMLC_ROLE"] in ["worker", "joint"]:
        if is_joint_mode():
            os.environ["DMLC_ROLE"] = "joint"
        # launch workers
        if "NVIDIA_VISIBLE_DEVICES" in os.environ:
            local_size = len(os.environ["NVIDIA_VISIBLE_DEVICES"].split(","))
        else:
            local_size = 1
        t = [None] * local_size

        bind_to_cores = os.getenv("BYTEPS_NUMA_ON", "1") == "1"
        if bind_to_cores:
            user_override = os.getenv("BYTEPS_VISIBLE_CPU_CORES", "").strip()
            if user_override:
                allocations = parse_num_range(user_override)
            else:
                allocations = allocate_cpu(local_size)

        for i in range(local_size):
            command = ' '.join(sys.argv[1:])
            if bind_to_cores:
                t[i] = PropagatingThread(idx=i, callback=done_callback,
                    target=worker_fn,
                    args=[i, local_size, command, allocations[i]])
            else:
                t[i] = PropagatingThread(idx=i, callback=done_callback,
                    target=worker_fn, args=[i, local_size, command])
            t[i].daemon = True
            t[i].start()

        join_threads(t)
        return

    if os.environ.get("BYTEPS_FORCE_DISTRIBUTED", "0") == "0" and \
       int(os.environ.get("DMLC_NUM_WORKER", "1")) == 1:
           # there's only one worker, and not forcing distributed mode
           return

    command = "python3 -c 'import byteps.server'"
    if os.environ["DMLC_ROLE"] == "scheduler":
        my_env = os.environ.copy()
        my_env['PS_VERBOSE'] = my_env.get('PS_VERBOSE', '1')
        ucx_src_addr = get_ucx_src_addr()
        if ucx_src_addr:
            my_env["UCX_RDMA_CM_SOURCE_ADDRESS"] = ucx_src_addr[0]
        subprocess.check_call(command, env=my_env,
                              stdout=sys.stdout, stderr=sys.stderr, shell=True)
        return

    if is_joint_mode():
        # do nothing when DMLC_ROLE is "server" in joint mode.
        return

    # now it's the servers in non-colocate mode
    local_size = 1

    t = [None] * local_size
    for i in range(local_size):
        t[i] = PropagatingThread(target=server_fn, args=[
                i, local_size, command])
        t[i].daemon = True
        t[i].start()
    for i in range(local_size):
        t[i].join()

if __name__ == "__main__":
    # for ps-lite RDMAVan IPC compatibility
    os.environ["BYTEPS_ENCODING_SCHEME_VERSION"] = os.environ.get("BYTEPS_ENCODING_SCHEME_VERSION", "1")
    if is_joint_mode():
        os.environ["DMLC_NUM_WORKER"] = str(int(os.environ["BYTEPS_NUM_NODES"]) *
                                            int(os.environ["BYTEPS_LOCAL_SIZE"]))
        os.environ["DMLC_NUM_SERVER"] = os.environ["DMLC_NUM_WORKER"]
    launch_bps()
