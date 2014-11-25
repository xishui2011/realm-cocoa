#!/usr/bin/python
##############################################################################
#
# Copyright 2014 Realm Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http:#www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
##############################################################################

# In the lldb shell, load with:
# command script import [Realm path]/plugin/lldb.py --allow-reload
# To load automatically, add that line to your ~/.lldbinit file (which you will
# have to create if you have not set up any previous lldb scripts), or run this
# file as a Python script outside of Xcode to install it automatically

if __name__ == '__main__':
    # Script is being run directly, so install it
    import errno
    import shutil
    import os

    source = os.path.realpath(__file__)
    destination = os.path.expanduser("~/Library/Application Support/Realm")

    # Copy the file into place
    try:
        os.makedirs(destination, 0744)
    except os.error as e:
        # It's fine if the directory already exists
        if e.errno != errno.EEXIST:
            raise

    shutil.copy2(source, destination + '/rlm_lldb.py')

    # Add it to ~/.lldbinit
    load_line = 'command script import "~/Library/Application Support/Realm/rlm_lldb.py" --allow-reload\n'
    is_installed = False
    try:
        with open(os.path.expanduser('~/.lldbinit')) as f:
            for line in f:
                if line == load_line:
                    is_installed = True
                    break
    except IOError as e:
        if e.errno != errno.ENOENT:
            raise
        # File not existing yet is fine

    if not is_installed:
        with open(os.path.expanduser('~/.lldbinit'), 'a') as f:
            f.write(load_line)

    exit(0)

import lldb
import re

def cache_lookup(cache, key, generator):
    value = cache.get(key, None)
    if not value:
        value = generator(key)
        if value:
            cache[key] = value
    return value

def unsigned(value):
    data = value.data
    if data.GetByteSize() == 4:
        return value.data.GetUnsignedInt32(lldb.SBError(), 0)
    return value.data.GetUnsignedInt64(lldb.SBError(), 0)

def frame(obj):
    # obj.GetSelectedThread() sometimes returns an invalid object for some reason
    return obj.process.GetSelectedThread().GetSelectedFrame()

def address(obj):
    return obj.GetData().GetAddress(lldb.SBError(), 0)

object_table_ptr_offset = None
def is_object_deleted(obj):
    def field_offset(type_name, field_name):
        for f in obj.target.FindFirstType(type_name).fields:
            if f.name == field_name:
                return f.byte_offset

    addr = address(obj)

    global object_table_ptr_offset
    if not object_table_ptr_offset:
        v = frame(obj).EvaluateExpression(
                'RLMDebugGetIvarOffset({}, "_row")'.format(path(obj)))
        object_table_ptr_offset = unsigned(v) + field_offset('tightdb::RowBase', 'm_table')

    ptr = obj.GetProcess().ReadUnsignedFromMemory(addr + object_table_ptr_offset,
            obj.target.addr_size, lldb.SBError())
    return ptr == 0

def frame_is_swift(obj):
    return frame(obj).compile_unit.file.fullpath.endswith('swift')

def path(obj, obj_type=None):
    if obj_type:
        obj_type = re.sub(r'RLMAccessor_v\d+_([^ ]+)', r'\1', obj_type)
        if frame_is_swift(obj):
            return '(RLMDebugAddrToObj({}) as {})'.format(str(address(obj)), obj_type.rstrip(' *'))
        return '(({})RLMDebugAddrToObj({}))'.format(obj_type, str(address(obj)))
    return 'RLMDebugAddrToObj(' + str(address(obj)) + ')'

ivar_offset_cache = {}
def get_ivars(obj, *args):
    def get_offset(type_name):
        ivars = {}
        for ivar in args:
            if obj.GetAddress():
                v = frame(obj).EvaluateExpression(
                        'RLMDebugGetIvarOffset({}, "_{}")'.format(path(obj), ivar))
            else:
                v = frame(obj).EvaluateExpression(
                        'RLMDebugGetIvarOffset(RLMDebugAddrToObj({}), "_{}")'.format(address(obj), ivar))
            v = unsigned(v)
            if v == 0:
                return None
            ivars[ivar] = v
        return ivars

    return cache_lookup(ivar_offset_cache, obj.type.name, get_offset)

type_cache = {}
def get_type(obj, name):
    def do_get_type(_):
        t = obj.target.FindFirstType(name)
        if not t and name.endswith('*'):
            t = obj.target.FindFirstType(name.rstrip('*')).GetPointerType()
        return t
    return cache_lookup(type_cache, name, do_get_type)

class IvarHelper(object):
    def __init__(self, obj, *ivars):
        self.obj = obj
        self.ivars = get_ivars(obj, *ivars)

    def _eval(self, expr):
        return frame(self.obj).EvaluateExpression(expr)

    def _to_str(self, val):
        return self.obj.GetProcess().ReadCStringFromMemory(val, 65536, lldb.SBError())

    def _value_from_ivar(self, ivar, ivar_type='id'):
        assert(self.ivars[ivar] > 0)
        return self.obj.CreateChildAtOffset(ivar, self.ivars[ivar], get_type(self.obj, ivar_type))

schema_cache = {}
class RLMObject_SyntheticChildrenProvider(IvarHelper):
    def __init__(self, obj, _):
        self.props = []
        self.ivars = None

        # if is_object_deleted(obj):
        #     print 'deleted'
        #     return

        super(RLMObject_SyntheticChildrenProvider, self).__init__(
                obj, 'objectSchema', 'realm')

        if not self.ivars:
            print 'no ivars'
            return

        object_schema = self._value_from_ivar('objectSchema', 'RLMObjectSchema*').deref
        def get_schema(_):
            v = self._eval('RLMDebugPropertyNames({})'.format(path(obj)))
            return self._to_str(unsigned(v)).split(' ')

        self.props = cache_lookup(schema_cache, str(object_schema.GetAddress()), get_schema)

    def num_children(self):
        return len(self.props) + 2 if self.ivars else 0

    def has_children(self):
        return self.ivars and len(self.props) and not is_object_deleted(self.obj)

    def get_child_index(self, name):
        if not self.ivars:
            return None
        if name == 'realm':
            return 0
        if name == 'objectSchema':
            return 1
        return self.props.index(name) + 2

    def get_child_at_index(self, index):
        if not self.ivars or index > len(self.props) + 2:
            return None
        if index == 0:
            return self._value_from_ivar('realm')
        if index == 1:
            return self._value_from_ivar('objectSchema')

        name = self.props[index - 2]
        if self.obj.thread:
            # thread isn't set correctly for objects from EvaluateExpression
            p = path(self.obj, self.obj.type.name) + '.' + name
            v = self.obj.CreateValueFromExpression(name, p)
        else:
            # And this doesn't work for optionals not from EvaluateExpression
            p = self.obj.path.replace('.Some', '!') + '.' + name
            v = frame(self.obj).EvaluateExpression(p)

        if 'RLMArray' in v.type.name:
            v = self.obj.CreateValueFromData(name, v.GetData(), get_type(self.obj, 'id'))
        else:
            v = self.obj.CreateValueFromData(name, v.GetData(), v.type)
        return v

def RLM_SummaryProvider(obj, _):
    addr = unsigned(frame(obj).EvaluateExpression('RLMDebugSummary({})'.format(path(obj))))
    if addr == 0:
        return None
    return obj.GetProcess().ReadCStringFromMemory(addr, 1024, lldb.SBError())

class RLMArray_SyntheticChildrenProvider(IvarHelper):
    def __init__(self, valobj, _):
        super(RLMArray_SyntheticChildrenProvider, self).__init__(valobj, 'realm')
        self.type = get_type(self.obj, 'id')

    def num_children(self):
        if not self.ivars:
            return None
        if not self.count:
            self.count = unsigned(self._eval("RLMDebugArrayCount({})".format(path(self.obj))))
        return self.count + 1

    def has_children(self):
        return self.ivars != None

    def get_child_index(self, name):
        if not self.ivars:
            return None
        if name == 'Some' or name == 'value':
            return None
        if name == 'realm':
            return 0
        return int(name.lstrip('[').rstrip(']')) + 1

    def get_child_at_index(self, index):
        if not self.ivars:
            return None
        if index == 0:
            return self._value_from_ivar('realm')

        key = '[' + str(index - 1) + ']'

        v = self.obj.CreateValueFromExpression(key, 'RLMDebugArrayChildAtIndex({}, {})'.format(path(self.obj), index - 1))

        value = self._eval('RLMDebugArrayChildAtIndex({}, {})'.format(path(self.obj), index - 1))
        data = self.obj.CreateValueFromData(key, value.GetData(), self.type)
        return data

    def update(self):
        self.count = None

class InitializerHack(object):
    def __init__(self, obj, _):
        self.obj = obj
        self.count = obj.GetNumChildren()

        obj.target.debugger.HandleCommand('type category delete RealmInit')

        addr = unsigned(frame(obj).EvaluateExpression('RLMDebugGetSubclassList()'))
        if addr == 0:
            # The first call to EvaluateExpression in a Swift context often
            # fails with an error in auto-import, but then works every
            # subsequent time
            addr = unsigned(frame(obj).EvaluateExpression('RLMDebugGetSubclassList()'))

        classes = obj.process.ReadCStringFromMemory(addr, 65536, lldb.SBError())
        for cls in classes.split(' '):
            if len(cls):
                obj.target.debugger.HandleCommand('type summary add -w Realm {} -F rlm_lldb.RLM_SummaryProvider'.format(cls))
                obj.target.debugger.HandleCommand('type synthetic add -w Realm {} --python-class rlm_lldb.RLMObject_SyntheticChildrenProvider'.format(cls))

    def num_children(self):
        return self.count

    def has_children(self):
        return self.count > 0

    def get_child_index(self, name):
        for i in xrange(self.obj.GetNumChildren()):
            if self.obj.GetChildAtIndex(i).name == name:
                return i

    def get_child_at_index(self, index):
        return self.obj.GetChildAtIndex(index)

def __lldb_init_module(debugger, _):
    debugger.HandleCommand('type summary add -w Realm RLMArray -F rlm_lldb.RLM_SummaryProvider')
    debugger.HandleCommand('type summary add -w Realm RLMArrayLinkView -F rlm_lldb.RLM_SummaryProvider')
    debugger.HandleCommand('type summary add -w Realm RLMResults -F rlm_lldb.RLM_SummaryProvider')
    debugger.HandleCommand('type summary add -w Realm -x RLMAccessor_ -F rlm_lldb.RLM_SummaryProvider')

    debugger.HandleCommand('type synthetic add -w Realm RLMArray --python-class rlm_lldb.RLMArray_SyntheticChildrenProvider')
    debugger.HandleCommand('type synthetic add -w Realm RLMArrayLinkView --python-class rlm_lldb.RLMArray_SyntheticChildrenProvider')
    debugger.HandleCommand('type synthetic add -w Realm RLMResults --python-class rlm_lldb.RLMArray_SyntheticChildrenProvider')
    debugger.HandleCommand('type synthetic add -w Realm -x RLMAccessor_.* --python-class rlm_lldb.RLMObject_SyntheticChildrenProvider')

    debugger.HandleCommand('type synthetic add -w RealmInit -x .* --python-class rlm_lldb.InitializerHack')
    debugger.GetCategory('RealmInit').SetEnabled(True)
    debugger.GetCategory('Realm').SetEnabled(True)
