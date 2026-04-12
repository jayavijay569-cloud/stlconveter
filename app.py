"""
ForgeConvert Backend v5.1 — Flask + Trimesh + Cascadio + AI Chatbot
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
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

def get_ext(fn): return os.path.splitext(fn.lower())[1]
def allowed(fn): return get_ext(fn) in ALLOWED_EXT


@app.route('/health')
def health():
    results = {'status': 'ok', 'formats': sorted(list(ALLOWED_EXT))}
    try:
        import trimesh
        results['trimesh'] = trimesh.__version__
    except Exception as e:
        results['trimesh'] = f'ERROR: {e}'
    try:
        import cascadio
        results['cascadio_attrs'] = [x for x in dir(cascadio) if not x.startswith('_')]
    except Exception as e:
        results['cascadio'] = f'ERROR: {e}'
    return jsonify(results)


def _to_single_mesh(loaded):
    import trimesh
    if isinstance(loaded, trimesh.Scene):
        meshes = [g for g in loaded.geometry.values()
                  if isinstance(g, trimesh.Trimesh) and len(g.faces) > 0]
        if not meshes:
            raise RuntimeError("Scene has no mesh geometry.")
        return trimesh.util.concatenate(meshes) if len(meshes) > 1 else meshes[0]
    elif isinstance(loaded, trimesh.Trimesh):
        return loaded
    else:
        raise RuntimeError(f"Unknown geometry type: {type(loaded)}")


def _export_stl(tm, out_path):
    import trimesh
    tm.remove_duplicate_faces()
    tm.remove_degenerate_faces()
    if not tm.is_watertight:
        trimesh.repair.fill_holes(tm)
        trimesh.repair.fix_normals(tm)
    stl_bytes = tm.export(file_type='stl')
    with open(out_path, 'wb') as f:
        f.write(stl_bytes)
    if os.path.getsize(out_path) == 0:
        raise RuntimeError("Conversion produced empty STL.")


def convert_to_stl(in_path: str, out_path: str):
    import trimesh

    ext = get_ext(in_path)
    print(f'[DEBUG] ext={ext}, file={in_path}')

    if ext in ('.step', '.stp', '.iges', '.igs', '.brep'):
        try:
            import cascadio
            attrs = [x for x in dir(cascadio) if not x.startswith('_')]
            print(f'[DEBUG] cascadio attrs: {attrs}')

            glb_path = in_path + '.glb'

            if hasattr(cascadio, 'step_to_glb'):
                cascadio.step_to_glb(in_path, glb_path)
            elif hasattr(cascadio, 'convert'):
                cascadio.convert(in_path, glb_path)
            else:
                raise RuntimeError(f"No known cascadio function. Available: {attrs}")

            loaded = trimesh.load(glb_path)
            tm = _to_single_mesh(loaded)
            try: os.remove(glb_path)
            except: pass

        except ImportError:
            print('[DEBUG] cascadio not available, using trimesh directly')
            loaded = trimesh.load(in_path, force='mesh')
            tm = _to_single_mesh(loaded)
        except Exception as e:
            print(f'[DEBUG] cascadio failed: {traceback.format_exc()}')
            raise RuntimeError(f"STEP/IGES conversion failed: {e}")
    else:
        loaded = trimesh.load(in_path, force='mesh')
        tm = _to_single_mesh(loaded)

    if tm is None or len(tm.faces) == 0:
        raise RuntimeError("No geometry found in file.")

    _export_stl(tm, out_path)


@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f or not f.filename:
        return jsonify({'error': 'Empty filename'}), 400
    filename = secure_filename(f.filename)
    if not allowed(filename):
        return jsonify({'error': f'Unsupported: {get_ext(filename)}'}), 415

    jid      = str(uuid.uuid4())
    ext      = get_ext(filename)
    in_path  = os.path.join(UPLOAD_FOLDER, f'{jid}_in{ext}')
    out_path = os.path.join(UPLOAD_FOLDER, f'{jid}_out.stl')
    out_name = os.path.splitext(filename)[0] + '.stl'

    try:
        f.save(in_path)
        print(f'[Convert] {filename} ({os.path.getsize(in_path)//1024} KB)')
        convert_to_stl(in_path, out_path)
        print(f'[Convert] OK → {out_name}')
        return send_file(out_path, mimetype='application/octet-stream',
                         as_attachment=True, download_name=out_name)
    except Exception as e:
        print(f'[Convert] FAILED:\n{traceback.format_exc()}')
        return jsonify({'error': str(e)}), 500
    finally:
        for p in [in_path, out_path]:
            try:
                if os.path.exists(p): os.remove(p)
            except: pass


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
            if not allowed(filename):
                errors.append(f'{filename}: unsupported'); continue
            jid      = str(uuid.uuid4())
            ext      = get_ext(filename)
            in_path  = os.path.join(UPLOAD_FOLDER, f'{jid}_in{ext}')
            out_path = os.path.join(UPLOAD_FOLDER, f'{jid}_out.stl')
            out_name = os.path.splitext(filename)[0] + '.stl'
            try:
                f.save(in_path)
                convert_to_stl(in_path, out_path)
                zf.write(out_path, out_name)
            except Exception as e:
                errors.append(f'{filename}: {e}')
                print(f'[Bulk] FAILED {filename}: {traceback.format_exc()}')
            finally:
                for p in [in_path, out_path]:
                    try:
                        if os.path.exists(p): os.remove(p)
                    except: pass
    zip_buf.seek(0)
    if zip_buf.getbuffer().nbytes <= 22:
        return jsonify({'error': 'All failed', 'details': errors}), 500
    resp = send_file(zip_buf, mimetype='application/zip',
                     as_attachment=True, download_name='ForgeConvert_STL.zip')
    if errors: resp.headers['X-Errors'] = ' | '.join(errors)
    return resp


@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    if not data or 'message' not in data:
        return jsonify({'error': 'No message'}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'ANTHROPIC_API_KEY not set'}), 500
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
            headers={'x-api-key': ANTHROPIC_API_KEY,
                     'anthropic-version': '2023-06-01',
                     'content-type': 'application/json'},
            json={'model': 'claude-haiku-4-5-20251001', 'max_tokens': 1024,
                  'system': 'You are ForgeBot, a CAD and 3D printing expert for ForgeConvert. Help with STEP, IGES, OBJ, FBX, GLB to STL conversion and 3D printing. Be concise.',
                  'messages': messages},
            timeout=30
        )
        resp.raise_for_status()
        return jsonify({'reply': resp.json()['content'][0]['text']})
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Timeout'}), 504
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/')
def index():
    return app.send_static_file('index.html')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'ForgeConvert v5.1 on http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
