"""
ForgeConvert Backend v6.0
STEP/STP/IGES → STL : trimesh + cascadio
Chatbot        : Google Gemini API (free)
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
                 '.obj', '.glb', '.gltf', '.ply', '.3mf', '.dae'}

app.config['MAX_CONTENT_LENGTH'] = MAX_MB * 1024 * 1024
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

def get_ext(fn): return os.path.splitext(fn.lower())[1]
def allowed(fn): return get_ext(fn) in ALLOWED_EXT


# ── Conversion ────────────────────────────────────────────────
def convert_to_stl(in_path: str, out_path: str):
    import trimesh
    ext = get_ext(in_path)

    if ext in ('.step', '.stp', '.iges', '.igs'):
        # Try cascadio first (best for STEP/IGES)
        try:
            import cascadio
            glb_path = in_path + '.glb'
            if hasattr(cascadio, 'step_to_glb'):
                cascadio.step_to_glb(in_path, glb_path)
            elif hasattr(cascadio, 'convert'):
                cascadio.convert(in_path, glb_path)
            else:
                raise ImportError("No usable cascadio function")
            loaded = trimesh.load(glb_path)
            try: os.remove(glb_path)
            except: pass
        except Exception as e:
            print(f'[cascadio failed] {e} — trying trimesh direct')
            loaded = trimesh.load(in_path, force='mesh')
    else:
        loaded = trimesh.load(in_path, force='mesh')

    # Handle Scene or Trimesh
    if isinstance(loaded, trimesh.Scene):
        meshes = [g for g in loaded.geometry.values()
                  if isinstance(g, trimesh.Trimesh) and len(g.faces) > 0]
        if not meshes:
            raise RuntimeError("No mesh geometry found in file.")
        tm = trimesh.util.concatenate(meshes) if len(meshes) > 1 else meshes[0]
    elif isinstance(loaded, trimesh.Trimesh):
        tm = loaded
    else:
        raise RuntimeError(f"Unknown geometry type: {type(loaded)}")

    if len(tm.faces) == 0:
        raise RuntimeError("Mesh has no faces.")

    # Clean mesh
    tm.remove_duplicate_faces()
    tm.remove_degenerate_faces()

    # Export STL
    stl_bytes = tm.export(file_type='stl')
    with open(out_path, 'wb') as f:
        f.write(stl_bytes)

    if os.path.getsize(out_path) == 0:
        raise RuntimeError("Conversion produced empty STL.")


# ── Routes ────────────────────────────────────────────────────
@app.route('/health')
def health():
    info = {'status': 'ok', 'formats': sorted(ALLOWED_EXT)}
    try:
        import trimesh; info['trimesh'] = trimesh.__version__
    except Exception as e:
        info['trimesh'] = f'ERROR: {e}'
    try:
        import cascadio; info['cascadio'] = 'ok'
    except Exception as e:
        info['cascadio'] = f'missing: {e}'
    return jsonify(info)


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
    if not GEMINI_API_KEY:
        return jsonify({'error': 'GEMINI_API_KEY not set'}), 500

    user_message = data.get('message', '').strip()[:2000]
    history      = data.get('history', [])[-10:]

    SYSTEM = (
        'You are ForgeBot, a helpful CAD and 3D printing expert for ForgeConvert. '
        'Help users with STEP, IGES, OBJ, GLB to STL conversion and 3D printing tips. '
        'Be concise and practical.'
    )

    contents = []
    for h in history:
        if h.get('role') == 'user':
            contents.append({'role': 'user', 'parts': [{'text': h['content']}]})
        elif h.get('role') == 'assistant':
            contents.append({'role': 'model', 'parts': [{'text': h['content']}]})
    contents.append({'role': 'user', 'parts': [{'text': user_message}]})

    try:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent?key={GEMINI_API_KEY}'
        resp = requests.post(url, json={
            'system_instruction': {'parts': [{'text': SYSTEM}]},
            'contents': contents,
            'generationConfig': {'maxOutputTokens': 1024, 'temperature': 0.7}
        }, timeout=30)
        resp.raise_for_status()
        reply = resp.json()['candidates'][0]['content']['parts'][0]['text']
        return jsonify({'reply': reply})
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Timeout. Try again.'}), 504
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/google2a3c218694012ee4.html')
def google_verify():
    return 'google-site-verification: google2a3c218694012ee4.html'

@app.route('/sitemap.xml')        
def sitemap():
    return '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://online-stl-converter.onrender.com/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>''', 200, {'Content-Type': 'application/xml'}

@app.route('/')
def index():
    return app.send_static_file('index.html')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'ForgeConvert v6.0 on http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
