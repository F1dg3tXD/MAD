bl_info = {
    "name": "MAD (Microphone Audio Driver)",
    "author": "F1dg3t",
    "version": (0, 1, 6),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > MAD",
    "description": "Use your Microphone as an Animation Driver in Blender.",
    "category": "Animation",
}

import bpy
from . import mad

def register():
    mad.register()

def unregister():
    mad.unregister()
