# Copyright 2019 Bytedance Inc. or its affiliates. All Rights Reserved.
# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
# Modifications copyright (C) 2018 Uber Technologies, Inc.
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
# =============================================================================

import ctypes
import os
import sysconfig
import atexit


def get_ext_suffix():
    """Determine library extension for various versions of Python."""
    ext_suffix = sysconfig.get_config_var('EXT_SUFFIX')
    if ext_suffix:
        return ext_suffix

    ext_suffix = sysconfig.get_config_var('SO')
    if ext_suffix:
        return ext_suffix

    return '.so'


def get_extension_full_path(pkg_path, *args):
    assert len(args) >= 1
    dir_path = os.path.join(os.path.dirname(pkg_path), *args[:-1])
    full_path = os.path.join(dir_path, args[-1] + get_ext_suffix())
    return full_path


def check_extension(ext_name, ext_env_var, pkg_path, *args):
    full_path = get_extension_full_path(pkg_path, *args)
    if not os.path.exists(full_path):
        raise ImportError(
            'Extension %s has not been built.  If this is not expected, reinstall '
            'BytePS with %s=1 to debug the build error.' % (ext_name, ext_env_var))


class BytePSBasics(object):
    """Wrapper class for the basic BytePS API."""

    def __init__(self, pkg_path, *args):
        full_path = get_extension_full_path(pkg_path, *args)
        self.C_LIB_CTYPES = ctypes.CDLL(full_path, mode=ctypes.RTLD_GLOBAL)
        # set C_API interfaces
        self.C_LIB_CTYPES.byteps_get_telemetry_size.restype = None
        self.C_LIB_CTYPES.byteps_get_telemetry_size.argtypes = (ctypes.POINTER(ctypes.c_int),)
        c_float_p = ctypes.POINTER(ctypes.c_float)
        c_int_p = ctypes.POINTER(ctypes.c_int32)
        c_char_p_p = ctypes.POINTER(ctypes.c_char_p)
        self.C_LIB_CTYPES.byteps_get_telemetry_data.restype = None
        self.C_LIB_CTYPES.byteps_get_telemetry_data.argtypes = c_char_p_p, c_float_p, c_float_p, c_int_p, c_int_p, ctypes.c_int32

    def init(self, lazy=True):
        """A function that inits BytePS."""
        atexit.register(self.shutdown)
        # UCX-van related env vars
        os.environ['UCX_SOCKADDR_CM_ENABLE'] = os.environ.get('UCX_SOCKADDR_CM_ENABLE', 'y')
        os.environ['PSLITE_UCX_USE_MT_MUTEX'] = os.environ.get('PSLITE_UCX_USE_MT_MUTEX', 'y')
        os.environ['PSLITE_UCX_RNDV_THRESH'] = os.environ.get('PSLITE_UCX_RNDV_THRESH', '8k')
        os.environ['BYTEPS_UCX_SHORT_THRESH'] = os.environ.get('BYTEPS_UCX_SHORT_THRESH', '0')
        if lazy:
            ret = self.C_LIB_CTYPES.byteps_lazy_init()
            return ret
        else:
            ret = self.C_LIB_CTYPES.byteps_init()
            return ret

    def shutdown(self):
        """A function that shuts BytePS down."""
        return self.C_LIB_CTYPES.byteps_shutdown()

    def is_initialized(self):
        """Returns True if BytePS is initialized"""
        is_initialized = self.C_LIB_CTYPES.byteps_is_initialized()
        return bool(is_initialized)

    def suspend(self):
        """A function that suspends BytePS for elastic training."""
        return self.C_LIB_CTYPES.byteps_suspend()

    def resume(self, num_workers, num_servers, global_rank, context=None):
        """A function that restarts BytePS after being suspended, for elastic training."""
        # set DMLC environment variables here
        os.environ['DMLC_NUM_WORKER'] = str(num_workers)
        os.environ['DMLC_NUM_SERVER'] = str(num_servers)
        os.environ['BYTEPS_GLOBAL_RANK'] = str(global_rank)
        return self.C_LIB_CTYPES.byteps_resume(num_workers, num_servers)

    def size(self):
        """A function that returns the number of BytePS processes.
        Returns:
          An integer scalar containing the number of BytePS processes.
        """
        size = self.C_LIB_CTYPES.byteps_size()
        if size == -1:
            raise ValueError(
                'BytePS has not been initialized; use bps.init().')
        return size

    def local_size(self):
        """A function that returns the number of BytePS processes within the
        node the current process is running on.
        Returns:
          An integer scalar containing the number of local BytePS processes.
        """
        local_size = self.C_LIB_CTYPES.byteps_local_size()
        if local_size == -1:
            raise ValueError(
                'BytePS has not been initialized; use bps.init().')
        return local_size

    def rank(self):
        """A function that returns the BytePS rank of the calling process.
        Returns:
          An integer scalar with the BytePS rank of the calling process.
        """
        rank = self.C_LIB_CTYPES.byteps_rank()
        if rank == -1:
            raise ValueError(
                'BytePS has not been initialized; use bps.init().')
        return rank

    def local_rank(self):
        """A function that returns the local BytePS rank of the calling process, within the
        node that it is running on. For example, if there are seven processes running
        on a node, their local ranks will be zero through six, inclusive.
        Returns:
          An integer scalar with the local BytePS rank of the calling process.
        """
        local_rank = self.C_LIB_CTYPES.byteps_local_rank()
        if local_rank == -1:
            raise ValueError(
                'BytePS has not been initialized; use bps.init().')
        return local_rank

    def get_pushpull_speed(self):
        """A function that returns the current push pull speed. Speed is
        calculated every 10 seconds.
          Returns:
            A tuple: (ms since epoch, speed in MegaBytes per second)
        """
        raise NotImplementedError("get_pushpull_speed() is deprecated. Please use get_telemetry instead")

    def get_telemetry(self, size=None):
        """A function that returns the current telemetry statistics.

          Args:
            size:
              Set the limit on the number of distinct byteps operations
              whose telemetries are returned. If set to None, all byteps
              operations' telemetries are returned.

          Returns:
            A list of tuples: (name, duration mean(us), duration stdev(us), occurrences)
        """
        if size is None:
            size_ptr = (ctypes.c_int*1)()
            self.C_LIB_CTYPES.byteps_get_telemetry_size(size_ptr)
            size = list(size_ptr)[0]
        assert size >= 0, size
        if size == 0:
            return []
        name_ptr = (ctypes.c_char_p*size)()
        mean_ptr = (ctypes.c_float*size)()
        stdev_ptr = (ctypes.c_float*size)()
        count_ptr = (ctypes.c_int32*size)()
        actual_size_ptr = (ctypes.c_int*1)()
        self.C_LIB_CTYPES.byteps_get_telemetry_data(name_ptr, mean_ptr, stdev_ptr,
                                                    count_ptr, actual_size_ptr, size)
        actual_size = list(actual_size_ptr)[0]
        result = list(zip(name_ptr, mean_ptr, stdev_ptr, count_ptr))[:actual_size]
        if result is None:
            result = []
        return result
