"""
ForgeConvert Backend — Flask + Trimesh + AI Chatbot
STEP/STP/IGES/OBJ/GLB/GLTF/FBX → STL : trimesh (pip install trimesh)
OBJ/GLB/GLTF  → also handled by frontend
Chatbot        → Claude API (anthropic)
"""

import os, io, uuid, zipfile, tempfile, traceback, requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

MAX_MB        = 500
UPLOAD_FOLDER = tempfile.gettempdir()
ALLOWED_EXT   = {'.step', '.stp', '.iges', '.igs', '.stl',
                 '.obj', '.glb', '.gltf', '.fbx', '.3ds',
                 '.dae', '.ply', '.3mf', '.brep', '.dxf'}

app.config['MAX_CONTENT_LENGTH'] = MAX_MB * 1024 * 1024

# Claude API key — Render lo environment variable ga set cheyyandi
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


def get_ext(fn): return os.path.splitext(fn.lower())[1]
def allowed(fn): return get_ext(fn) in ALLOWED_EXT


# ── Core: Trimesh conversion ──────────────────────────────────
def convert_to_stl(in_path: str, out_path: str):
    """
    Uses trimesh to load any supported 3D format and export as binary STL.
    Supports: STEP, STP, IGES, OBJ, GLB, GLTF, FBX, DAE, PLY, 3MF, STL, DXF
    For STEP/IGES: uses cascadio (Open CASCADE) via trimesh if available,
    otherwise falls back to trimesh loader.
    """
    import trimesh
    import numpy as np

    ext = get_ext(in_path)

    # STEP / STP / IGES / IGS / BREP — cascadio: STEP → GLB → trimesh
    if ext in ('.step', '.stp', '.iges', '.igs', '.brep'):
        try:
            import cascadio
            # correct cascadio API: step_to_glb(in_path, out_path)
            glb_path = in_path + '.glb'
            cascadio.step_to_glb(in_path, glb_path)
            loaded = trimesh.load(glb_path)
            tm = _to_single_mesh(loaded)
            try:
                os.remove(glb_path)
            except:
                pass
        except ImportError:
            # cascadio not installed — trimesh direct (limited STEP support)
            loaded = trimesh.load(in_path, force='mesh')
            tm = _to_single_mesh(loaded)
        except Exception as e:
            raise RuntimeError(f"STEP/IGES conversion failed: {e}")
    else:
        # OBJ, GLB, GLTF, FBX, DAE, PLY, 3MF, STL — trimesh handles directly
        loaded = trimesh.load(in_path, force='mesh')
        tm = _to_single_mesh(loaded)

    if tm is None or len(tm.faces) == 0:
        raise RuntimeError("No geometry found — file may be empty or corrupt.")

    # Fix mesh issues
    tm.remove_duplicate_faces()
    tm.remove_degenerate_faces()
    if not tm.is_watertight:
        trimesh.repair.fill_holes(tm)
        trimesh.repair.fix_normals(tm)

    # Export binary STL
    stl_bytes = tm.export(file_type='stl')
    with open(out_path, 'wb') as f:
        f.write(stl_bytes)

    if os.path.getsize(out_path) == 0:
        raise RuntimeError("Conversion produced empty STL — check input file.")


def _to_single_mesh(loaded):
    """Convert trimesh Scene or Geometry to a single Trimesh object."""
    import trimesh
    import numpy as np

    if isinstance(loaded, trimesh.Scene):
        # Merge all meshes in the scene
        meshes = [g for g in loaded.geometry.values()
                  if isinstance(g, trimesh.Trimesh) and len(g.faces) > 0]
        if not meshes:
            raise RuntimeError("Scene contains no mesh geometry.")
        if len(meshes) == 1:
            return meshes[0]
        # Concatenate all meshes
        return trimesh.util.concatenate(meshes)
    elif isinstance(loaded, trimesh.Trimesh):
        return loaded
    else:
        raise RuntimeError(f"Unexpected geometry type: {type(loaded)}")


# ── Routes ────────────────────────────────────────────────────
@app.route('/health')
def health():
    try:
        import trimesh
        trimesh_ok = True
        trimesh_ver = trimesh.__version__
    except:
        trimesh_ok = False
        trimesh_ver = 'not installed'

    try:
        import cascadio
        cascadio_ok = True
    except:
        cascadio_ok = False

    return jsonify({
        'status'      : 'ok',
        'trimesh'     : trimesh_ok,
        'trimesh_ver' : trimesh_ver,
        'cascadio'    : cascadio_ok,
        'formats'     : sorted(list(ALLOWED_EXT))
    })


@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    f = request.files['file']
    if not f or not f.filename:
        return jsonify({'error': 'Empty filename'}), 400

    filename = secure_filename(f.filename)
    if not allowed(filename):
        return jsonify({
            'error': f'Unsupported format: {get_ext(filename)}. '
                     f'Supported: {", ".join(sorted(ALLOWED_EXT))}'
        }), 415

    jid      = str(uuid.uuid4())
    ext      = get_ext(filename)
    in_path  = os.path.join(UPLOAD_FOLDER, f'{jid}_in{ext}')
    out_name = os.path.splitext(filename)[0] + '.stl'
    out_path = os.path.join(UPLOAD_FOLDER, f'{jid}_out.stl')

    try:
        f.save(in_path)
        size_kb = os.path.getsize(in_path) // 1024
        print(f'[Convert] {filename} ({size_kb} KB)')

        convert_to_stl(in_path, out_path)

        out_size = os.path.getsize(out_path)
        print(f'[Convert] OK → {out_name} ({out_size // 1024} KB)')

        return send_file(
            out_path,
            mimetype='application/octet-stream',
            as_attachment=True,
            download_name=out_name
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        for p in [in_path, out_path]:
            try:
                if os.path.exists(p): os.remove(p)
            except:
                pass


@app.route('/convert-bulk', methods=['POST'])
def convert_bulk():
    uploaded = request.files.getlist('files')
    if not uploaded:
        return jsonify({'error': 'No files'}), 400

    zip_buf = io.BytesIO()
    errors  = []

    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in uploaded:
            filename = secure_filename(f.filename or 'file')
            ext      = get_ext(filename)

            if not allowed(filename):
                errors.append(f'{filename}: unsupported format')
                continue

            jid      = str(uuid.uuid4())
            in_path  = os.path.join(UPLOAD_FOLDER, f'{jid}_in{ext}')
            out_path = os.path.join(UPLOAD_FOLDER, f'{jid}_out.stl')
            out_name = os.path.splitext(filename)[0] + '.stl'

            try:
                f.save(in_path)
                convert_to_stl(in_path, out_path)
                zf.write(out_path, out_name)
                print(f'[Bulk] {filename} OK')
            except Exception as e:
                errors.append(f'{filename}: {e}')
                traceback.print_exc()
            finally:
                for p in [in_path, out_path]:
                    try:
                        if os.path.exists(p): os.remove(p)
                    except:
                        pass

    zip_buf.seek(0)
    if zip_buf.getbuffer().nbytes <= 22:
        return jsonify({'error': 'All conversions failed', 'details': errors}), 500

    resp = send_file(
        zip_buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name='ForgeConvert_STL_Export.zip'
    )
    if errors:
        resp.headers['X-Errors'] = ' | '.join(errors)
    return resp


@app.route('/chat', methods=['POST'])
def chat():
    """
    AI Chatbot — CAD/3D printing expert
    POST body: { "message": "user message", "history": [...] }
    """
    data = request.get_json()
    if not data or 'message' not in data:
        return jsonify({'error': 'No message provided'}), 400

    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'ANTHROPIC_API_KEY not configured on server'}), 500

    user_message = data.get('message', '').strip()[:2000]
    history      = data.get('history', [])[-10:]

    messages = []
    for h in history:
        if h.get('role') in ('user', 'assistant') and h.get('content'):
            messages.append({'role': h['role'], 'content': h['content']})
    messages.append({'role': 'user', 'content': user_message})

    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key'        : ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type'     : 'application/json',
            },
            json={
                'model'     : 'claude-haiku-4-5-20251001',
                'max_tokens': 1024,
                'system'    : (
                    'You are ForgeBot, a helpful CAD and 3D printing expert assistant '
                    'for ForgeConvert — a tool that converts STEP, STP, OBJ, IGES, FBX, GLB files to STL. '
                    'Help users with: file format questions, 3D printing tips, CAD software advice, '
                    'conversion issues, and STL troubleshooting. '
                    'Keep answers concise and practical. '
                    'If asked about unrelated topics, politely redirect to CAD/3D printing.'
                ),
                'messages': messages,
            },
            timeout=30
        )
        resp.raise_for_status()
        result = resp.json()
        reply  = result['content'][0]['text']
        return jsonify({'reply': reply})

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out. Please try again.'}), 504
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/')
def index():
    return app.send_static_file('index.html')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'ForgeConvert v5.0 (Trimesh engine) on http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
