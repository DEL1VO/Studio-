"""
Запускается внутри Blender:
  blender -b -P blender_worker.py -- --scene scene.json --out result.mp4 [--preview]

Для каждого персонажа (PNG с прозрачностью/однотонным фоном):
  1. плоскость с текстурой, сетка для деформации;
  2. автоскелет (корпус, голова, руки, ноги), автоматические веса;
  3. анимация: дыхание, жесты рук, кивки, моргание;
  4. рот: спрайт-овал, открывается по громкости аудио (липсинк);
Рендер: Eevee, ортокамера (2D-вид), звук сводится ffmpeg.
"""
import json
import math
import subprocess
import sys
import wave
from pathlib import Path

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
arg = lambda k: argv[argv.index(k) + 1]
SCENE = json.loads(Path(arg("--scene")).read_text())
OUT = Path(arg("--out"))
PREVIEW = "--preview" in argv
FPS = 24
RES = (640, 360) if PREVIEW else (1920, 1080)

# Относительные координаты лица на картинке (0..1, от левого нижнего угла).
# Подходят для типичного полнорост. персонажа; точнее — через mediapipe (см. detect_face).
FACE = {"mouth": (0.5, 0.80), "eye_l": (0.44, 0.87), "eye_r": (0.56, 0.87)}

PALETTE = {"classic": (0.96, 0.89, 0.74, 1), "modern": (0.55, 0.75, 0.9, 1),
           "storybook": (0.85, 0.92, 0.8, 1)}


def detect_face(png: str):
    """Если в Blender доступен mediapipe — уточняем рот/глаза, иначе берём FACE."""
    try:
        import mediapipe as mp  # noqa
        import cv2
        img = cv2.imread(png)
        res = mp.solutions.face_mesh.FaceMesh(static_image_mode=True).process(
            cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        if not res.multi_face_landmarks:
            return FACE
        lm = res.multi_face_landmarks[0].landmark
        pt = lambda i: (lm[i].x, 1 - lm[i].y)
        return {"mouth": pt(13), "eye_l": pt(159), "eye_r": pt(386)}
    except Exception:
        return FACE


def amplitude(wav_path: str):
    """Громкость по кадрам (0..1) для липсинка."""
    with wave.open(wav_path) as w:
        sr, raw = w.getframerate(), w.readframes(w.getnframes())
    import array
    a = array.array("h", raw)
    step = max(1, sr // FPS)
    vals = [sum(abs(x) for x in a[i:i + step]) / step for i in range(0, len(a), step)]
    peak = max(vals) or 1
    return [min(1.0, v / peak * 1.4) for v in vals]


def clean():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "BLENDER_EEVEE_NEXT" if hasattr(bpy.types, "SceneEEVEE") and \
        bpy.app.version >= (4, 2) else "BLENDER_EEVEE"
    sc.render.fps = FPS
    sc.render.resolution_x, sc.render.resolution_y = RES
    sc.render.film_transparent = False
    return sc


def make_camera(sc):
    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = 6
    cam.location = (0, -10, 1.5)
    cam.rotation_euler = (math.pi / 2, 0, 0)
    sc.collection.objects.link(cam)
    sc.camera = cam


def make_background(sc, style, setting):
    bpy.ops.mesh.primitive_plane_add(size=14, location=(0, 2, 1.5), rotation=(math.pi / 2, 0, 0))
    bg = bpy.context.object
    mat = bpy.data.materials.new("bg")
    mat.use_nodes = True
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = PALETTE.get(style, PALETTE["classic"])
    bg.data.materials.append(mat)
    # TODO: здесь подставляется сгенерированный/загруженный фон под setting


def textured_plane(png, name, height=3.2):
    img = bpy.data.images.load(png)
    ratio = img.size[0] / img.size[1]
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=12, y_subdivisions=18, size=1)
    ob = bpy.context.object
    ob.name = name
    ob.rotation_euler = (math.pi / 2, 0, 0)
    ob.scale = (height * ratio, height, 1)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    ob.location = (0, 0, height / 2)
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    bsdf = nt.nodes["Principled BSDF"]
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
    mat.blend_method = "BLEND" if hasattr(mat, "blend_method") else None
    ob.data.materials.append(mat)
    return ob, height, height * ratio


def auto_rig(ob, h, w):
    """Скелет по пропорциям тела + автоматические веса."""
    bpy.ops.object.armature_add(location=ob.location)
    arm = bpy.context.object
    arm.name = ob.name + "_rig"
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones
    eb.remove(eb[0])
    z0 = ob.location.z - h / 2

    def bone(name, head, tail, parent=None):
        b = eb.new(name)
        b.head, b.tail = Vector(head), Vector(tail)
        if parent:
            b.parent = eb[parent]
            b.use_connect = False
        return b

    X = lambda f: (f - 0.5) * w
    Z = lambda f: z0 + f * h
    bone("hips", (0, 0, Z(.45)), (0, 0, Z(.55)))
    bone("spine", (0, 0, Z(.55)), (0, 0, Z(.72)), "hips")
    bone("neck", (0, 0, Z(.72)), (0, 0, Z(.78)), "spine")
    bone("head", (0, 0, Z(.78)), (0, 0, Z(.97)), "neck")
    for side, s in (("L", -1), ("R", 1)):
        bone(f"arm_{side}", (X(.5 + s * .08), 0, Z(.70)), (X(.5 + s * .25), 0, Z(.55)), "spine")
        bone(f"forearm_{side}", (X(.5 + s * .25), 0, Z(.55)), (X(.5 + s * .33), 0, Z(.42)), f"arm_{side}")
        bone(f"leg_{side}", (X(.5 + s * .05), 0, Z(.45)), (X(.5 + s * .06), 0, Z(.22)), "hips")
        bone(f"shin_{side}", (X(.5 + s * .06), 0, Z(.22)), (X(.5 + s * .06), 0, Z(.02)), f"leg_{side}")
    bpy.ops.object.mode_set(mode="OBJECT")
    ob.select_set(True)
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    return arm


def make_face_sprites(ob, meta, h, w):
    """Плоскости с готовыми эмоциями лица (из листа ChatGPT), переключаются покадрово."""
    z0 = ob.location.z - h / 2
    x0, yb, x1, yt = meta["face_rect"]
    cx, cz = (x0 + x1) / 2, (yb + yt) / 2
    fw = (x1 - x0) * w * 1.25
    sprites = {}
    for idx in (0, 3, 4, 5, 8):          # нейтр., улыбка, рот открыт, удивление, грусть
        if idx >= len(meta["faces"]):
            continue
        img = bpy.data.images.load(meta["faces"][idx])
        ratio = img.size[0] / img.size[1]
        bpy.ops.mesh.primitive_plane_add(size=1, rotation=(math.pi / 2, 0, 0))
        o = bpy.context.object
        o.name = f"face_{idx}"
        o.scale = (fw, 1, fw / ratio)
        o.location = ((cx - .5) * w, -0.03 - idx * 0.001, z0 + cz * h - fw / ratio * 0.05)
        m = bpy.data.materials.new(o.name)
        m.use_nodes = True
        nt = m.node_tree
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = img
        b = nt.nodes["Principled BSDF"]
        nt.links.new(tex.outputs["Color"], b.inputs["Base Color"])
        nt.links.new(tex.outputs["Alpha"], b.inputs["Alpha"])
        o.data.materials.append(m)
        sprites[idx] = o
    return sprites


def animate_faces(sprites, amp, start, frames, emotion):
    """Какой спрайт лица виден в каждом кадре: громко -> рот открыт, иначе эмоция."""
    quiet = {"радостно": 3, "весело": 3, "улыбка": 3, "удивлённо": 5, "удивленно": 5,
             "грустно": 8}.get(emotion.lower(), 0)
    for f in range(frames):
        a = amp[min(f, len(amp) - 1)]
        show = 4 if a > 0.5 else (3 if a > 0.25 and quiet in (0, 3) else quiet)
        if show not in sprites:
            show = 0
        for idx, o in sprites.items():
            o.hide_render = o.hide_viewport = (idx != show)
            o.keyframe_insert("hide_render", frame=start + f + 1)
            o.keyframe_insert("hide_viewport", frame=start + f + 1)


def make_face_parts(ob, png, h, w):
    face = detect_face(png)
    z0 = ob.location.z - h / 2

    def at(rel, name, sx, sy, color):
        bpy.ops.mesh.primitive_circle_add(vertices=24, fill_type="NGON", radius=1,
                                          rotation=(math.pi / 2, 0, 0))
        o = bpy.context.object
        o.name = name
        o.location = ((rel[0] - .5) * w, -0.02, z0 + rel[1] * h)
        o.scale = (sx, 1, sy)
        m = bpy.data.materials.new(name)
        m.use_nodes = True
        m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = color
        o.data.materials.append(m)
        return o

    mouth = at(face["mouth"], "mouth", .07, .005, (.25, .05, .05, 1))
    lid_l = at(face["eye_l"], "lid_l", .05, .0, (.9, .75, .6, 1))
    lid_r = at(face["eye_r"], "lid_r", .05, .0, (.9, .75, .6, 1))
    return mouth, lid_l, lid_r


def animate(arm, mouth, lids, amp, frames, emotion, start=0):
    """Дыхание, жесты, кивки, моргание, рот по громкости."""
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="POSE")
    pb = arm.pose.bones
    for f in range(0, frames, 6):
        t = (start + f) / FPS
        pb["spine"].rotation_mode = pb["head"].rotation_mode = "XYZ"
        pb["spine"].rotation_euler = (0, math.sin(t * 2) * .02, 0)
        pb["spine"].keyframe_insert("rotation_euler", frame=start + f + 1)
        pb["head"].rotation_euler = (0, math.sin(t * 3) * .05 * (1 + amp[min(f, len(amp) - 1)]), 0)
        pb["head"].keyframe_insert("rotation_euler", frame=start + f + 1)
        for side, ph in (("L", 0), ("R", 1.5)):
            pb[f"arm_{side}"].rotation_mode = "XYZ"
            pb[f"arm_{side}"].rotation_euler = (0, math.sin(t * 2.5 + ph) * .15 *
                                                (1 + amp[min(f, len(amp) - 1)]), 0)
            pb[f"arm_{side}"].keyframe_insert("rotation_euler", frame=start + f + 1)
    bpy.ops.object.mode_set(mode="OBJECT")
    # липсинк
    if mouth is None:
        return
    for f in range(frames):
        a = amp[min(f, len(amp) - 1)]
        mouth.scale[2] = .005 + a * .045
        mouth.keyframe_insert("scale", frame=start + f + 1)
    # моргание раз в ~3 с
    for b in range(10, frames, FPS * 3):
        for lid in lids:
            for fr, v in ((b, 0), (b + 2, .05), (b + 4, 0)):
                lid.scale[2] = v
                lid.keyframe_insert("scale", frame=start + fr + 1)


def main():
    sc = clean()
    make_camera(sc)
    sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
    sun.rotation_euler = (math.radians(60), 0, math.radians(20))
    sc.collection.objects.link(sun)

    cursor = 0
    audio_parts = []
    for s_i, scene in enumerate(SCENE["scenes"]):
        make_background(sc, SCENE["style"], scene["setting"])
        chars = {}
        for ln in scene["lines"]:
            if ln["who"] not in chars:
                png = ln["image"]
                ob, h, w = textured_plane(png, ln["who"])
                arm = auto_rig(ob, h, w)
                meta = ln.get("meta") or {}
                idx = len(chars)
                shift = (idx - .5) * 2.6 if len(set(l["who"] for l in scene["lines"])) > 1 else 0
                if meta.get("faces"):
                    sprites = make_face_sprites(ob, meta, h, w)
                    for o in (ob, arm, *sprites.values()):
                        o.location.x += shift
                    chars[ln["who"]] = (arm, None, sprites)
                else:
                    mouth, ll, lr = make_face_parts(ob, png, h, w)
                    for o in (ob, arm, mouth, ll, lr):
                        o.location.x += shift
                    chars[ln["who"]] = (arm, mouth, (ll, lr))
        for ln in scene["lines"]:
            frames = max(int(ln["duration"] * FPS) + 6, 12)
            arm, mouth, lids = chars[ln["who"]]
            amp = amplitude(ln["audio"])
            if mouth is None:           # лицо из листа эмоций
                animate(arm, None, (), amp, frames, ln["emotion"], start=cursor)
                animate_faces(lids, amp, cursor, frames, ln["emotion"])
            else:
                animate(arm, mouth, lids, amp, frames, ln["emotion"], start=cursor)
            audio_parts.append((ln["audio"], cursor / FPS))
            cursor += frames
    sc.frame_start, sc.frame_end = 1, max(cursor, 24)

    silent = OUT.with_suffix(".silent.mp4")
    sc.render.image_settings.file_format = "FFMPEG"
    sc.render.ffmpeg.format = "MPEG4"
    sc.render.ffmpeg.codec = "H264"
    sc.render.filepath = str(silent)
    bpy.ops.render.render(animation=True)

    # сведение аудио
    cmd = ["ffmpeg", "-y", "-i", str(silent)]
    for a, _ in audio_parts:
        cmd += ["-i", a]
    filt = "".join(f"[{i + 1}]adelay={int(t * 1000)}|{int(t * 1000)}[a{i}];"
                   for i, (_, t) in enumerate(audio_parts))
    filt += "".join(f"[a{i}]" for i in range(len(audio_parts))) + f"amix=inputs={len(audio_parts)}[aout]"
    cmd += ["-filter_complex", filt, "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-shortest", str(OUT)]
    subprocess.run(cmd, check=True)


main()
