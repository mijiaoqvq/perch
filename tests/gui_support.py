"""Native GTK snapshot helper; no access to the rest of the desktop."""
from gi.repository import Gtk

def capture_widget(destination, widget):
    # Use the native GTK renderer to capture only this application window.
    import ctypes
    import ctypes.util
    from gi.repository import Gsk, Graphene
    snapshot = Gtk.Snapshot()
    paintable = Gtk.WidgetPaintable.new(widget)
    paintable.snapshot(snapshot, widget.get_width(), widget.get_height())
    lib = ctypes.CDLL(ctypes.util.find_library('gtk-4'))
    capsule = ctypes.pythonapi.PyCapsule_GetPointer
    capsule.argtypes = [ctypes.py_object, ctypes.c_char_p]
    capsule.restype = ctypes.c_void_p
    ptr = lambda obj: capsule(obj.__gpointer__, None)
    lib.gtk_snapshot_to_node.argtypes = [ctypes.c_void_p]
    lib.gtk_snapshot_to_node.restype = ctypes.c_void_p
    node = lib.gtk_snapshot_to_node(ptr(snapshot))
    if not node:
        widget.queue_draw()
        widget.present()
        return False
    renderer = widget.get_native().get_renderer()
    lib.gsk_renderer_render_texture.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.gsk_renderer_render_texture.restype = ctypes.c_void_p
    texture = lib.gsk_renderer_render_texture(ptr(renderer), node, None)
    lib.gdk_texture_save_to_png.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.gdk_texture_save_to_png.restype = ctypes.c_bool
    assert lib.gdk_texture_save_to_png(texture, str(destination).encode())
    lib.gsk_render_node_unref.argtypes = [ctypes.c_void_p]
    lib.gsk_render_node_unref(node)
    lib.g_object_unref.argtypes = [ctypes.c_void_p]
    lib.g_object_unref(texture)
    print('SCREENSHOT', destination, flush=True)
    return True
