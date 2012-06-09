# Volatility
# Copyright (c) 2008 Volatile Systems
# Copyright (c) 2008 Brendan Dolan-Gavitt <bdolangavitt@wesleyan.edu>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#
import copy

from volatility import obj
from volatility import addrspace

from volatility.plugins.overlays import basic
from volatility.plugins.overlays.windows import pe_vtypes
from volatility.plugins.windows import kdbgscan


# Standard vtypes are usually autogenerated by scanning through header
# files, collecting debugging symbol data etc. This file defines
# fixups and improvements to the standard types.
windows_overlay = {
    '_EPROCESS' : [ None, {
    'CreateTime' : [ None, ['WinTimeStamp', {}]],
    'ExitTime' : [ None, ['WinTimeStamp', {}]],
    'InheritedFromUniqueProcessId' : [ None, ['unsigned int']],
    'ImageFileName' : [ None, ['String', dict(length = 16)]],
    'UniqueProcessId' : [ None, ['unsigned int']],
    }],

    '_ETHREAD' : [ None, {
    'CreateTime' : [ None, ['ThreadCreateTimeStamp', {}]],
    'ExitTime' : [ None, ['WinTimeStamp', {}]],
    }],

    '_OBJECT_SYMBOLIC_LINK' : [ None, {
    'CreationTime' : [ None, ['WinTimeStamp', {}]],
    }],

    '_KUSER_SHARED_DATA' : [ None, {
    'SystemTime' : [ None, ['WinTimeStamp', dict(is_utc = True)]],
    'TimeZoneBias' : [ None, ['WinTimeStamp', {}]],
    }],

    # The DTB is really an array of 2 ULONG_PTR but we only need the first one
    # which is the value loaded into CR3. The second one, according to procobj.c
    # of the wrk-v1.2, contains the PTE that maps something called hyper space.
    '_KPROCESS' : [ None, {
    'DirectoryTableBase' : [ None, ['unsigned long']],
    }],

    '_HANDLE_TABLE_ENTRY' : [ None, {
    'Object' : [ None, ['_EX_FAST_REF']],
    }],

    '_IMAGE_SECTION_HEADER' : [ None, {
    'Name' : [ 0x0, ['String', dict(length = 8)]],
    }],

    'PO_MEMORY_IMAGE' : [ None, {
    'Signature':   [ None, ['String', dict(length = 4)]],
    'SystemTime' : [ None, ['WinTimeStamp', {}]],
    }],

    '_DBGKD_GET_VERSION64' : [  None, {
    'DebuggerDataList' : [ None, ['pointer', ['unsigned long']]],
    }],

    '_CM_KEY_NODE' : [ None, {
    'Signature' : [ None, ['String', dict(length = 2)]],
    'LastWriteTime' : [ None, ['WinTimeStamp', {}]],
    'Name' : [ None, ['String', dict(length = lambda x: x.NameLength)]],
    }],

    '_CM_NAME_CONTROL_BLOCK' : [ None, {
    'Name' : [ None, ['String', dict(length = lambda x: x.NameLength)]],
    }],

    '_CHILD_LIST' : [ None, {
    'List' : [ None, ['pointer', ['array', lambda x: x.Count,
                                 ['pointer', ['_CM_KEY_VALUE']]]]],
    }],

    '_CM_KEY_VALUE' : [ None, {
    'Signature' : [ None, ['String', dict(length = 2)]],
    'Name' : [ None, ['String', dict(length = lambda x: x.NameLength)]],
    }],

    '_CM_KEY_INDEX' : [ None, {
    'Signature' : [ None, ['String', dict(length = 2)]],
    'List' : [ None, ['array', lambda x: x.Count.v() * 2, ['pointer', ['_CM_KEY_NODE']]]],
    }],

    '_PHYSICAL_MEMORY_DESCRIPTOR' : [ None, {
    'Run' : [ None, ['array', lambda x: x.NumberOfRuns, ['_PHYSICAL_MEMORY_RUN']]],
    }],

    '_TOKEN' : [ None, {
    'UserAndGroups' : [ None, ['pointer', ['array', lambda x: x.UserAndGroupCount,
                                 ['_SID_AND_ATTRIBUTES']]]],
    }],

    '_SID' : [ None, {
    'SubAuthority' : [ None, ['array', lambda x: x.SubAuthorityCount, ['unsigned long']]],
    }],

    '_CLIENT_ID': [ None, {
    'UniqueProcess' : [ None, ['unsigned int']],
    'UniqueThread' : [ None, ['unsigned int']],
    }],

    '_MMVAD': [ None, {
    # This is the location of the MMVAD type which controls how to parse the
    # node. It is located before the structure.
    'Tag': [-4 , ['String', dict(length = 4)]],
    }],

    '_MMVAD_SHORT': [ None, {
    'Tag': [-4 , ['String', dict(length = 4)]],
    }],

    '_MMVAD_LONG': [ None, {
    'Tag': [-4 , ['String', dict(length = 4)]],
    }],
}

class _UNICODE_STRING(obj.CType):
    """Class representing a _UNICODE_STRING

    Adds the following behavior:
      * The Buffer attribute is presented as a Python string rather
        than a pointer to an unsigned short.
      * The __str__ method returns the value of the Buffer.
    """

    def v(self, vm=None):
        length = self.Length.v(vm=vm)
        if length > 0 and length <= 1024:
            data = self.Buffer.dereference_as('UnicodeString', length=length, vm=vm)
            return data.v()
        else:
            return ''

    def __nonzero__(self):
        ## Unicode strings are valid if they point at a valid memory
        return bool(self.Buffer)

    def __format__(self, formatspec):
        return format(self.v(), formatspec)

    def __str__(self):
        return self.v() or ''



class _EPROCESS(obj.CType):
    """ An extensive _EPROCESS with bells and whistles """
    @property
    def Peb(self):
        """ Returns a _PEB object which is using the process address space.

        The PEB structure is referencing back into the process address
        space so we need to switch address spaces when we look at
        it. This method ensure this happens automatically.
        """
        process_ad = self.get_process_address_space()
        if process_ad:
            offset = self.m("Peb").v()
            peb = self.obj_profile.Object(theType="_PEB", offset=offset, vm = process_ad,
                                          name = "Peb", parent = self)

            if peb.is_valid():
                return peb

        return obj.NoneObject("Peb not found")

    @property
    def IsWow64(self):
        """Returns True if this is a wow64 process"""
        return hasattr(self, 'Wow64Process') and self.Wow64Process.v() != 0

    @property
    def SessionId(self):
        """Returns the Session ID of the process"""

        if self.Session.is_valid():
            process_space = self.get_process_address_space()
            if process_space:
                return self.obj_profile.Object("_MM_SESSION_SPACE",
                                               offset = self.Session,
                                               vm = process_space).SessionId

        return obj.NoneObject("Cannot find process session")

    def get_process_address_space(self):
        """ Gets a process address space for a task given in _EPROCESS """
        directory_table_base = self.Pcb.DirectoryTableBase.v()

        try:
            process_as = self.obj_vm.__class__(base=self.obj_vm.base,
                                               session=self.obj_vm.get_config(),
                                               dtb = directory_table_base, astype='virtual')
        except AssertionError, e:
            return obj.NoneObject("Unable to get process AS: %s" % e)

        process_as.name = "Process {0}".format(self.UniqueProcessId)

        return process_as

    def _get_modules(self, the_list, the_type):
        """Generator for DLLs in one of the 3 PEB lists"""
        if self.UniqueProcessId and the_list:
            for l in the_list.list_of_type("_LDR_DATA_TABLE_ENTRY", the_type):
                yield l

    def get_init_modules(self):
        return self._get_modules(self.Peb.Ldr.InInitializationOrderModuleList,
                                 "InInitializationOrderLinks")

    def get_mem_modules(self):
        return self._get_modules(self.Peb.Ldr.InMemoryOrderModuleList,
                                 "InMemoryOrderLinks")

    def get_load_modules(self):
        return self._get_modules(self.Peb.Ldr.InLoadOrderModuleList, "InLoadOrderLinks")

    def get_token(self):
        """Return the process's TOKEN object if its valid"""

        # The dereference checks if the address is valid
        # and returns obj.NoneObject if it fails
        token = self.Token.dereference_as("_TOKEN")

        # This check fails if the above dereference failed
        # or if any of the _TOKEN specific validity tests failed.
        if token.is_valid():
            return token

        return obj.NoneObject("Cannot get process Token")

    def ObReferenceObjectByHandle(self, handle, type=None):
        """Search the object table and retrieve the object by handle.

        Args:
          handle: The handle we search for.
          type: The object will be cast to this type.
        """
        for h in self.ObjectTable.handles():
            if h.HandleValue == handle:
                if type is None:
                    return h
                else:
                    return h.dereference_as(type)

        return obj.NoneObject("Could not find handle in ObjectTable")


class _TOKEN(obj.CType):
    """A class for Tokens"""

    def is_valid(self):
        """Override BaseObject.is_valid with some additional
        checks specific to _TOKEN objects."""
        return obj.CType.is_valid(self) and self.TokenInUse in (0, 1) and self.SessionId < 10

    def get_sids(self):
        """Generator for process SID strings"""
        if self.UserAndGroupCount < 0xFFFF:
            for sa in self.UserAndGroups.dereference():
                sid = sa.Sid.dereference_as('_SID')
                for i in sid.IdentifierAuthority.Value:
                    id_auth = i
                yield "S-" + "-".join(str(i) for i in (sid.Revision, id_auth) +
                                      tuple(sid.SubAuthority))


class _ETHREAD(obj.CType):
    """ A class for threads """

    def owning_process(self):
        """Return the EPROCESS that owns this thread"""
        return self.ThreadsProcess.dereference()

    def attached_process(self):
        """Return the EPROCESS that this thread is currently
        attached to."""
        return self.Tcb.ApcState.Process.dereference_as("_EPROCESS")

class _HANDLE_TABLE(obj.CType):
    """ A class for _HANDLE_TABLE.

    This used to be a member of _EPROCESS but it was isolated per issue
    91 so that it could be subclassed and used to service other handle
    tables, such as the _KDDEBUGGER_DATA64.PspCidTable.
    """

    def get_item(self, entry, handle_value = 0):
        """Returns the OBJECT_HEADER of the associated handle. The parent
        is the _HANDLE_TABLE_ENTRY so that an object can be linked to its
        GrantedAccess.
        """
        return entry.Object.dereference_as("_OBJECT_HEADER", parent = entry,
                                           handle_value = handle_value)

    def _make_handle_array(self, offset, level, depth = 0):
        """ Returns an array of _HANDLE_TABLE_ENTRY rooted at offset,
        and iterates over them.
        """
        # The counts below are calculated by taking the size of a page and dividing
        # by the size of the data type contained within the page. For more information
        # see http://blogs.technet.com/b/markrussinovich/archive/2009/09/29/3283844.aspx
        if level > 0:
            count = 0x1000 / self.obj_profile.get_obj_size("address")
            target = "address"
        else:
            count = 0x1000 / self.obj_profile.get_obj_size("_HANDLE_TABLE_ENTRY")
            target = "_HANDLE_TABLE_ENTRY"

        table = self.obj_profile.Object(theType="Array", offset = offset, vm = self.obj_vm,
                                        count = count, target = target,
                                        parent = self)

        if table:
            for entry in table:
                if not entry.is_valid():
                    break

                if level > 0:
                    ## We need to go deeper:
                    for h in self._make_handle_array(entry, level - 1, depth):
                        yield h
                    depth += 1
                else:

                    # All handle values are multiples of four, on both x86 and x64.
                    handle_multiplier = 4
                    # Calculate the starting handle value for this level.
                    handle_level_base = depth * count * handle_multiplier
                    # The size of a handle table entry.
                    handle_entry_size = self.obj_profile.get_obj_size("_HANDLE_TABLE_ENTRY")
                    # Finally, compute the handle value for this object.
                    handle_value = ((entry.obj_offset - offset) /
                                   (handle_entry_size / handle_multiplier)) + handle_level_base

                    ## OK We got to the bottom table, we just resolve
                    ## objects here:
                    item = self.get_item(entry, handle_value)

                    if item == None:
                        continue

                    try:
                        # New object header
                        if item.TypeIndex != 0x0:
                            yield item
                    except AttributeError:
                        if item.Type.Name:
                            yield item

    def handles(self):
        """ A generator which yields this process's handles

        _HANDLE_TABLE tables are multi-level tables at the first level
        they are pointers to second level table, which might be
        pointers to third level tables etc, until the final table
        contains the real _OBJECT_HEADER table.

        This generator iterates over all the handles recursively
        yielding all handles. We take care of recursing into the
        nested tables automatically.
        """
        # This should work equally for 32 and 64 bit systems
        LEVEL_MASK = 7

        TableCode = self.TableCode.v() & ~LEVEL_MASK
        table_levels = self.TableCode.v() & LEVEL_MASK
        offset = TableCode

        for h in self._make_handle_array(offset, table_levels):
            yield h


class _OBJECT_HEADER(obj.CType):
    """A Volatility object to handle Windows object headers.

    This object applies only to versions below windows 7.
    """

    optional_headers = [
        ('NameInfo', 'NameInfoOffset', '_OBJECT_HEADER_NAME_INFO'),
        ('HandleInfo', 'HandleInfoOffset', '_OBJECT_HEADER_HANDLE_INFO'),
        ('HandleInfo', 'QuotaInfoOffset', '_OBJECT_HEADER_QUOTA_INFO')]

    def __init__(self, handle_value=0, **kwargs):
        self.HandleValue = handle_value
        self._preamble_size = 0
        super(_OBJECT_HEADER, self).__init__(**kwargs)

        # Create accessors for optional headers
        self.find_optional_headers()

    def find_optional_headers(self):
        """Find this object's optional headers."""
        offset = self.obj_offset

        for name, name_offset, objtype in self.optional_headers:
            if self.obj_profile.has_type(objtype):
                header_offset = self.m(name_offset).v()
                if header_offset:
                    o = self.obj_profile.Object(theType=objtype,
                                                offset=offset - header_offset,
                                                vm = self.obj_vm)
                else:
                    o = obj.NoneObject("Header not set")

                self.newattr(name, o)

                # Optional headers stack before this _OBJECT_HEADER.
                if o:
                    self._preamble_size += o.size()

    def preamble_size(self):
        return self._preamble_size

    def size(self):
        """The size of the object header is actually the position of the Body
        element."""
        return self.obj_profile.get_obj_offset("_OBJECT_HEADER", "Body")

    @property
    def GrantedAccess(self):
        if self.obj_parent:
            return self.obj_parent.GrantedAccess
        return obj.NoneObject("No parent known")

    def dereference_as(self, theType, vm=None):
        """Instantiate an object from the _OBJECT_HEADER.Body"""
        return self.obj_profile.Object(theType=theType, offset=self.Body.obj_offset,
                                       vm=vm or self.obj_vm, parent=self)

    def get_object_type(self, kernel_address_space):
        """Return the object's type as a string"""
        type_obj = self.obj_profile.Object(
            theType="_OBJECT_TYPE", vm=kernel_address_space, offset=self.Type)

        return type_obj.Name.v()



class _FILE_OBJECT(obj.CType):
    """Class for file objects"""

    @property
    def AccessString(self):
        """Make a nicely formatted ACL string."""
        return (((self.ReadAccess > 0 and "R") or '-') +
                ((self.WriteAccess > 0  and "W") or '-') +
                ((self.DeleteAccess > 0 and "D") or '-') +
                ((self.SharedRead > 0 and "r") or '-') +
                ((self.SharedWrite > 0 and "w") or '-') +
                ((self.SharedDelete > 0 and "d") or '-'))

    def file_name_with_device(self):
        """Return the name of the file, prefixed with the name
        of the device object to which the file belongs"""
        name = ""
        if self.DeviceObject:
            object_hdr = self.obj_profile.Object(
                theType="_OBJECT_HEADER", offset=(
                    self.DeviceObject.v() - self.obj_profile.get_obj_offset(
                        "_OBJECT_HEADER", "Body")),
                vm=self.obj_vm)

            if object_hdr.NameInfo:
                name = u"\\Device\\{0}".format(object_hdr.NameInfo.Name)

        if self.FileName:
            name += self.FileName.v()

        return name

## This is an object which provides access to the VAD tree.
class _MMVAD(obj.CType):
    """Class factory for _MMVAD objects"""

    ## The actual type depends on this tag value.
    tag_map = {'Vadl': '_MMVAD_LONG',
               'VadS': '_MMVAD_SHORT',
               'Vad ': '_MMVAD_LONG',
               'VadF': '_MMVAD_SHORT',
               'Vadm': '_MMVAD_LONG',
              }

    def dereference(self, vm=None):
        """Return the exact type of this _MMVAD depending on the tag.

        All _MMVAD objects are initially instantiated as a generic _MMVAD
        object. However, depending on their tags, they really are one of the
        extended types, _MMVAD_LONG, or _MMVAD_SHORT.

        This method checks the type and returns the correct object. For invalid
        tags we return _MMVAD object.

        Returns:
          an _MMVAD_SHORT or _MMVAD_LONG object representing this _MMVAD.
        """
        # Get the tag and return the correct vad type if necessary
        real_type = self.tag_map.get(self.Tag.v(), None)
        if not real_type:
            return None

        return self.obj_profile.Object(
            theType=real_type, offset=self.obj_offset, profile=self.obj_profile,
            vm=vm or self.obj_vm, parent=self.obj_parent)

    def traverse(self, visited = None):
        """ Traverse the VAD tree by generating all the left items,
        then the right items.

        We try to be tolerant of cycles by storing all offsets visited.
        """
        if visited == None:
            visited = set()

        ## We try to prevent loops here
        if self.obj_offset in visited:
            return

        yield self

        for c in self.LeftChild.traverse(visited = visited):
            visited.add(c.obj_offset)
            yield c

        for c in self.RightChild.traverse(visited = visited):
            visited.add(c.obj_offset)
            yield c

    @property
    def Start(self):
        """Get the starting virtual address"""
        return self.StartingVpn << 12

    @property
    def End(self):
        """Get the ending virtual address"""
        return ((self.EndingVpn + 1) << 12) - 1

    @property
    def Parent(self):
        try:
            return self.m("Parent")
        except AttributeError:
            return obj.NoneObject("No parent known")


class _MMVAD_SHORT(_MMVAD):
    """Class with convenience functions for _MMVAD_SHORT functions"""


class _MMVAD_LONG(_MMVAD):
    """Class with convenience functions for _MMVAD_SHORT functions"""


class _EX_FAST_REF(obj.CType):
    """This type allows instantiating an object from its .Object member."""

    def __init__(self, target=None, **kwargs):
        self.target = target
        super(_EX_FAST_REF, self).__init__(**kwargs)

    def dereference(self, vm=None):
        if self.target is None:
            raise TypeError("No target specified for dereferencing an _EX_FAST_REF.")

        return self.dereference_as(self.target)

    def dereference_as(self, theType, parent = None, vm=None, **kwargs):
        """Use the _EX_FAST_REF.Object pointer to resolve an object of the
        specified type.
        """
        MAX_FAST_REF = self.obj_profile.constants['MAX_FAST_REF']
        return self.obj_profile.Object(theType=theType,
                                       offset=self.Object.v() & ~MAX_FAST_REF,
                                       vm=vm or self.obj_vm,
                                       parent = parent or self, **kwargs)


class ThreadCreateTimeStamp(basic.WinTimeStamp):
    """Handles ThreadCreateTimeStamps which are bit shifted WinTimeStamps"""
    def as_windows_timestamp(self):
        return obj.NativeType.v(self) >> 3


class _CM_KEY_BODY(obj.CType):
    """Registry key"""

    def full_key_name(self):
        output = []
        kcb = self.KeyControlBlock
        while kcb.ParentKcb:
            if kcb.NameBlock.Name == None:
                break
            output.append(str(kcb.NameBlock.Name))
            kcb = kcb.ParentKcb
        return "\\".join(reversed(output))

class _MMVAD_FLAGS(obj.CType):
    """This is for _MMVAD_SHORT.u.VadFlags"""
    def __str__(self):
        return ", ".join(["%s: %s" % (name, self.m(name)) for name in sorted(
                    self.members.keys()) if self.m(name) != 0])

class _MMVAD_FLAGS2(_MMVAD_FLAGS):
    """This is for _MMVAD_LONG.u2.VadFlags2"""
    pass

class _MMSECTION_FLAGS(_MMVAD_FLAGS):
    """This is for _CONTROL_AREA.u.Flags"""
    pass


import crash_vtypes
import kdbg_vtypes
import tcpip_vtypes
import ssdt_vtypes

# Reference:
# http://computer.forensikblog.de/en/2006/03/dmp-file-structure.html

crash_overlays = {
    "_DMP_HEADER": [None, {
            'Signature': [None, ['String', dict(length=4)]],
            'ValidDump': [None, ['String', dict(length=4)]],
            'SystemTime': [None, ['WinTimeStamp']],
            'DumpType': [None, ['Enumeration', {
                        'choices': {
                            1: "Full Dump",
                            2: "Kernel Dump",
                            },
                        'target': 'unsigned int'}]],
            }],
    }

crash_overlays['_DMP_HEADER64'] = copy.deepcopy(crash_overlays['_DMP_HEADER'])


class BaseWindowsProfile(basic.BasicWindowsClasses):
    """Common symbols for all of windows kernel profiles."""
    _md_os = "windows"

    def __init__(self, **kwargs):
        super(BaseWindowsProfile, self).__init__(**kwargs)

        # Crash support.
        self.add_types(crash_vtypes.crash_vtypes)
        self.add_overlay(crash_overlays)

        # KDBG types.
        self.add_types(kdbg_vtypes.kdbg_vtypes)
        self.add_overlay(kdbg_vtypes.kdbg_overlay)
        self.add_classes({
                "_KDDEBUGGER_DATA64": kdbg_vtypes._KDDEBUGGER_DATA64
                })

        self.add_types(tcpip_vtypes.tcpip_vtypes)
        self.add_types(ssdt_vtypes.ssdt_vtypes)
        self.add_classes({
            '_UNICODE_STRING': _UNICODE_STRING,
            '_EPROCESS': _EPROCESS,
            '_ETHREAD': _ETHREAD,
            '_HANDLE_TABLE': _HANDLE_TABLE,
            '_OBJECT_HEADER': _OBJECT_HEADER,
            '_FILE_OBJECT': _FILE_OBJECT,
            '_MMVAD': _MMVAD,
            '_MMVAD_SHORT': _MMVAD_SHORT,
            '_MMVAD_LONG': _MMVAD_LONG,
            '_EX_FAST_REF': _EX_FAST_REF,
            'ThreadCreateTimeStamp': ThreadCreateTimeStamp,
            '_CM_KEY_BODY': _CM_KEY_BODY,
            '_MMVAD_FLAGS': _MMVAD_FLAGS,
            '_MMVAD_FLAGS2': _MMVAD_FLAGS2,
            '_MMSECTION_FLAGS': _MMSECTION_FLAGS,
            })

        self.add_overlay(windows_overlay)

        # Also apply basic PE file parsing to the overlays.
        pe_vtypes.PEFileImplementation.Modify(self)
