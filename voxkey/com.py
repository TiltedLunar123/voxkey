"""The little bit of COM that ctypes does not do for you.

Two things in the app need a COM object with no pywin32 to lean on: writing a
Start menu shortcut, and asking UI Automation what has keyboard focus. Both
come down to CoCreateInstance and a handful of vtable calls, which is all this
is.
"""

from __future__ import annotations

import ctypes
import uuid
from ctypes import POINTER, byref, c_ubyte, c_ulong, c_ushort, c_void_p

CLSCTX_INPROC_SERVER = 1
COINIT_MULTITHREADED = 0
COINIT_APARTMENTTHREADED = 2

_RELEASE = 2


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", c_ubyte * 8),
    ]

    @classmethod
    def of(cls, text: str) -> "GUID":
        u = uuid.UUID(text)
        guid = cls()
        guid.Data1 = u.time_low
        guid.Data2 = u.time_mid
        guid.Data3 = u.time_hi_version
        guid.Data4 = (c_ubyte * 8)(*u.bytes[8:])
        return guid


def initialize(multithreaded: bool = False) -> None:
    """Join an apartment on this thread. Already being in one is not an error."""
    mode = COINIT_MULTITHREADED if multithreaded else COINIT_APARTMENTTHREADED
    try:
        ctypes.OleDLL("ole32").CoInitializeEx(None, mode)
    except OSError:
        pass  # initialised earlier on this thread, possibly in the other mode


def create(clsid: GUID, iid: GUID) -> c_void_p:
    obj = c_void_p()
    ctypes.OleDLL("ole32").CoCreateInstance(
        byref(clsid), None, CLSCTX_INPROC_SERVER, byref(iid), byref(obj)
    )
    return obj


def call(interface: c_void_p, slot: int, *args, argtypes=()) -> int:
    """Invoke a method through the object's vtable. A failed HRESULT raises
    OSError, which is what ctypes does for the HRESULT return type."""
    vtable_address = ctypes.cast(interface, POINTER(c_void_p))[0]
    vtable = ctypes.cast(c_void_p(vtable_address), POINTER(c_void_p))
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)
    return prototype(vtable[slot])(interface, *args)


def release(interface: c_void_p | None) -> None:
    if not interface:
        return
    try:
        vtable_address = ctypes.cast(interface, POINTER(c_void_p))[0]
        vtable = ctypes.cast(c_void_p(vtable_address), POINTER(c_void_p))
        ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtable[_RELEASE])(interface)
    except OSError:
        pass
