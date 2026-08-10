bl_info = {
    "name": "Blender Dev MCP",
    "author": "WXP",
    "version": (2, 1),
    # 3.2 is the real floor: the screenshot fallback uses
    # bpy.context.temp_override, which does not exist before it.
    "blender": (3, 2, 0),
    "location": "View3D > Sidebar > Dev MCP",
    "description": "Drive Blender from an MCP client for addon development and debugging",
    "category": "Development",
}

from .addon import register, unregister

__all__ = ["register", "unregister"]
