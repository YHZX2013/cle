import os
import struct

import archinfo
from . import Backend, Symbol, Section, register_backend
from .relocations import Relocation
from ..errors import CLEError
from ..address_translator import AT

try:
    import pefile
except ImportError:
    pefile = None

__all__ = ('PE',)

import logging
l = logging.getLogger('cle.pe')

# Reference: https://msdn.microsoft.com/en-us/library/ms809762.aspx


class WinSymbol(Symbol):
    """
    Represents a symbol for the PE format.
    """
    def __init__(self, owner, name, addr, is_import, is_export):
        super(WinSymbol, self).__init__(owner, name, addr, owner.arch.bytes, Symbol.TYPE_FUNCTION)
        self.is_import = is_import
        self.is_export = is_export

    @property
    def rebased_addr(self):
        # Windows does everything with RVAs, so override the default which assumes this is a LVA
        return AT.from_rva(self.addr, self.owner_obj).to_mva()


class WinReloc(Relocation):
    """
    Represents a relocation for the PE format.
    """
    def __init__(self, owner, symbol, addr, resolvewith, reloc_type=None, next_rva=None):
        super(WinReloc, self).__init__(owner, symbol, addr, None)
        self.resolvewith = resolvewith
        self.reloc_type = reloc_type
        self.next_rva = next_rva # only used for IMAGE_REL_BASED_HIGHADJ

    def resolve_symbol(self, solist, bypass_compatibility=False):
        if not bypass_compatibility:
            solist = [x for x in solist if self.resolvewith == x.provides]
        return super(WinReloc, self).resolve_symbol(solist)

    @property
    def value(self):
        if self.resolved:
            return self.resolvedby.rebased_addr

    @property
    def rebased_addr(self):
        # Windows does everything with RVAs, so override the default which assumes this is a LVA
        return AT.from_rva(self.addr, self.owner_obj).to_mva()

    def relocate(self, solist, bypass_compatibility=False):
        # no symbol -> this is a relocation described in the DIRECTORY_ENTRY_BASERELOC table
        if self.symbol is None:
            if self.reloc_type == pefile.RELOCATION_TYPE['IMAGE_REL_BASED_ABSOLUTE']:
                # no work required
                pass
            elif self.reloc_type == pefile.RELOCATION_TYPE['IMAGE_REL_BASED_HIGHLOW']:
                org_bytes = ''.join(self.owner_obj.memory.read_bytes(self.addr, 4))
                org_value = struct.unpack('<I', org_bytes)[0]
                rebased_value = AT.from_lva(org_value, self.owner_obj).to_mva()
                rebased_bytes = struct.pack('<I', rebased_value % 2**32)
                self.owner_obj.memory.write_bytes(self.addr, rebased_bytes)
            elif self.reloc_type == pefile.RELOCATION_TYPE['IMAGE_REL_BASED_DIR64']:
                org_bytes = ''.join(self.owner_obj.memory.read_bytes(self.addr, 8))
                org_value = struct.unpack('<Q', org_bytes)[0]
                rebased_value = AT.from_lva(org_value, self.owner_obj).to_mva()
                rebased_bytes = struct.pack('<Q', rebased_value)
                self.owner_obj.memory.write_bytes(self.addr, rebased_bytes)
            else:
                l.warning('PE contains unimplemented relocation type %d', self.reloc_type)
        else:
            return super(WinReloc, self).relocate(solist, bypass_compatibility)


class PESection(Section):
    """
    Represents a section for the PE format.
    """
    def __init__(self, pe_section, remap_offset=0):
        super(PESection, self).__init__(
            pe_section.Name,
            pe_section.Misc_PhysicalAddress,
            pe_section.VirtualAddress + remap_offset,
            pe_section.Misc_VirtualSize,
        )

        self.characteristics = pe_section.Characteristics

    #
    # Public properties
    #

    @property
    def is_readable(self):
        return self.characteristics & 0x40000000 != 0

    @property
    def is_writable(self):
        return self.characteristics & 0x80000000 != 0

    @property
    def is_executable(self):
        return self.characteristics & 0x20000000 != 0


class PE(Backend):
    """
    Representation of a PE (i.e. Windows) binary.
    """

    def __init__(self, *args, **kwargs):
        if pefile is None:
            raise CLEError("Install the pefile module to use the PE backend!")

        super(PE, self).__init__(*args, **kwargs)
        self.segments = self.sections # in a PE, sections and segments have the same meaning
        self.os = 'windows'
        if self.binary is None:
            self._pe = pefile.PE(data=self.binary_stream.read())
        else:
            self._pe = pefile.PE(self.binary)

        if self.arch is None:
            self.set_arch(archinfo.arch_from_id(pefile.MACHINE_TYPE[self._pe.FILE_HEADER.Machine]))

        self.mapped_base = self.linked_base = self._pe.OPTIONAL_HEADER.ImageBase
        self._entry = AT.from_rva(self._pe.OPTIONAL_HEADER.AddressOfEntryPoint, self).to_lva()

        if hasattr(self._pe, 'DIRECTORY_ENTRY_IMPORT'):
            self.deps = [entry.dll for entry in self._pe.DIRECTORY_ENTRY_IMPORT]
        else:
            self.deps = []

        if self.binary is not None and not self.is_main_bin:
            self.provides = os.path.basename(self.binary)
        else:
            self.provides = None

        self.tls_used = False
        self.tls_data_start = None
        self.tls_data_size = None
        self.tls_index_address = None
        self.tls_callbacks = None
        self.tls_size_of_zero_fill = None

        self._exports = {}
        self._symbol_cache = self._exports # same thing
        self._handle_imports()
        self._handle_exports()
        self._handle_relocs()
        self._register_tls()
        self._register_sections()
        self.linking = 'dynamic' if self.deps else 'static'

        self.jmprel = self._get_jmprel()

        self.memory.add_backer(0, self._pe.get_memory_mapped_image())

    @staticmethod
    def is_compatible(stream):
        identstring = stream.read(0x1000)
        stream.seek(0)
        if identstring.startswith('MZ') and len(identstring) > 0x40:
            peptr = struct.unpack('I', identstring[0x3c:0x40])[0]
            if peptr < len(identstring) and identstring[peptr:peptr + 4] == 'PE\0\0':
                return True
        return False

    #
    # Public methods
    #

    def get_symbol(self, name):
        return self._exports.get(name, None)

    @property
    def supports_nx(self):
        return self._pe.OPTIONAL_HEADER.DllCharacteristics & 0x100 != 0

    #
    # Private methods
    #

    def _get_jmprel(self):
        return self.imports

    def _handle_imports(self):
        if hasattr(self._pe, 'DIRECTORY_ENTRY_IMPORT'):
            for entry in self._pe.DIRECTORY_ENTRY_IMPORT:
                for imp in entry.imports:
                    imp_name = imp.name
                    if imp_name is None: # must be an import by ordinal
                        imp_name = "%s.ordinal_import.%d" % (entry.dll, imp.ordinal)
                    symb = WinSymbol(self, imp_name, 0, True, False)
                    reloc = WinReloc(self, symb, imp.address, entry.dll)
                    self.imports[imp_name] = reloc
                    self.relocs.append(reloc)

    def _handle_exports(self):
        if hasattr(self._pe, 'DIRECTORY_ENTRY_EXPORT'):
            symbols = self._pe.DIRECTORY_ENTRY_EXPORT.symbols
            for exp in symbols:
                symb = WinSymbol(self, exp.name, exp.address, False, True)
                self._exports[exp.name] = symb

    def _handle_relocs(self):
        if hasattr(self._pe, 'DIRECTORY_ENTRY_BASERELOC'):
            for base_reloc in self._pe.DIRECTORY_ENTRY_BASERELOC:
                entry_idx = 0
                while entry_idx < len(base_reloc.entries):
                    reloc_data = base_reloc.entries[entry_idx]
                    if reloc_data.type == pefile.RELOCATION_TYPE['IMAGE_REL_BASED_HIGHADJ']: #occupies 2 entries
                        if entry_idx == len(base_reloc.entries):
                            l.warning('PE contains corrupt relocation table')
                            break
                        next_entry = base_reloc.entries[entry_idx]
                        entry_idx += 1
                        reloc = WinReloc(self, None, reloc_data.rva, None, reloc_type=reloc_data.type, next_rva=next_entry.rva)
                    else:
                        reloc = WinReloc(self, None, reloc_data.rva, None, reloc_type=reloc_data.type)
                    self.relocs.append(reloc)
                    entry_idx += 1

    def _register_tls(self):
        if hasattr(self._pe, 'DIRECTORY_ENTRY_TLS'):
            tls = self._pe.DIRECTORY_ENTRY_TLS.struct

            self.tls_used = True
            self.tls_data_start = tls.StartAddressOfRawData
            self.tls_data_size = tls.EndAddressOfRawData - tls.StartAddressOfRawData
            self.tls_index_address = tls.AddressOfIndex
            self.tls_callbacks = self._register_tls_callbacks(tls.AddressOfCallBacks)
            self.tls_size_of_zero_fill = tls.SizeOfZeroFill

    def _register_tls_callbacks(self, addr):
        """
        TLS callbacks are stored as an array of virtual addresses to functions.
        The last entry is empty (NULL), which indicates the end of the table
        """
        callbacks = []

        callback_rva = AT.from_lva(addr, self).to_rva()
        callback = self._pe.get_dword_at_rva(callback_rva)
        while callback != 0:
            callbacks.append(callback)
            callback_rva += 4
            callback = self._pe.get_dword_at_rva(callback_rva)

        return callbacks

    def _register_sections(self):
        """
        Wrap self._pe.sections in PESection objects, and add them to self.sections.
        """

        for pe_section in self._pe.sections:
            section = PESection(pe_section, remap_offset=self.linked_base)
            self.sections.append(section)
            self.sections_map[section.name] = section

register_backend('pe', PE)
