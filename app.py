"""
ForgeConvert Backend — Flask + GMSH + AI Chatbot
STEP/STP/IGES → STL : gmsh (industrial grade, pip install gmsh)
OBJ/GLB/GLTF  → handled by frontend
Chatbot        → Claude API (anthropic)
"""

import os, io, uuid, zipfile, tempfile, traceback
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

MAX_MB        = 500
UPLOAD_FOLDER = tempfile.gettempdir()
ALLOWED_EXT   = {'.step', '.stp', '.iges', '.igs', '.stl', '.obj', '.brep'}
app.config['MAX_CONTENT_LENGTH'] = MAX_MB * 1024 * 1024

# Claude API key — Render lo environment variable ga set cheyyandi
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


def get_ext(fn): return os.path.splitext(fn.lower())[1]
def allowed(fn): return get_ext(fn) in ALLOWED_EXT


# ── Core: GMSH conversion ─────────────────────────────────────
def convert_to_stl(in_path: str, out_path: str):
    """
    Uses GMSH via subprocess to avoid threading/signal issues in Flask.
    Runs GMSH in a separate Python process — safe from any thread.
    """
    import subprocess, sys

    script = f"""
import gmsh, sys
gmsh.initialize(["-noterm", "-nopopup", "-v", "0"])
gmsh.option.setNumber("General.Terminal", 0)
try:
    gmsh.model.add("model")
    gmsh.merge({repr(in_path)})
    gmsh.model.occ.synchronize()
    gmsh.option.setNumber("Mesh.Algorithm", 6)
    gmsh.option.setNumber("Mesh.CharacteristicLengthFactor", 0.15)
    gmsh.option.setNumber("Mesh.CharacteristicLengthMin", 0)
    gmsh.option.setNumber("Mesh.CharacteristicLengthMax", 1e22)
    gmsh.option.setNumber("Mesh.RecombineAll", 0)
    gmsh.option.setNumber("Mesh.Optimize", 1)
    gmsh.option.setNumber("Mesh.OptimizeNetgen", 1)
    gmsh.option.setNumber("Mesh.SmoothRatio", 1.8)
    gmsh.option.setNumber("Mesh.AngleSmoothNormals", 30)
    gmsh.model.mesh.generate(2)
    gmsh.model.mesh.optimize("Netgen")
    gmsh.option.setNumber("Mesh.Binary", 1)
    gmsh.write({repr(out_path)})
    gmsh.finalize()
    sys.exit(0)
except Exception as e:
    gmsh.finalize()
    print("GMSH_ERROR:", e, file=sys.stderr)
    sys.exit(1)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120
    )

    if result.returncode != 0:
        err = result.stderr.strip().split("GMSH_ERROR:")[-1].strip()
        raise RuntimeError(f"GMSH: {err or result.stderr.strip()}")

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("GMSH produced empty output — file may be corrupt.")


# ── Routes ────────────────────────────────────────────────────
@app.route('/health')
def health():
    try:
        import gmsh
        gmsh_ok = True
    except:
        gmsh_ok = False
    return jsonify({'status': 'ok', 'gmsh': gmsh_ok, 'formats': list(ALLOWED_EXT)})


@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    f = request.files['file']
    if not f or not f.filename:
        return jsonify({'error': 'Empty filename'}), 400

    filename = secure_filename(f.filename)
    if not allowed(filename):
        return jsonify({'error': f'Unsupported format: {get_ext(filename)}. Supported: {", ".join(ALLOWED_EXT)}'}), 415

    jid      = str(uuid.uuid4())
    ext      = get_ext(filename)
    in_path  = os.path.join(UPLOAD_FOLDER, f'{jid}_in{ext}')
    out_name = os.path.splitext(filename)[0] + '.stl'
    out_path = os.path.join(UPLOAD_FOLDER, f'{jid}_out.stl')

    try:
        f.save(in_path)
        print(f'[Convert] {filename} ({os.path.getsize(in_path)//1024} KB)')
        convert_to_stl(in_path, out_path)
        size = os.path.getsize(out_path)
        print(f'[Convert] OK → {out_name} ({size//1024} KB)')
        return send_file(out_path, mimetype='application/octet-stream',
                         as_attachment=True, download_name=out_name)
    except Exception as e:
        traceback.print_exc()
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
            ext      = get_ext(filename)
            if not allowed(filename):
                errors.append(f'{filename}: unsupported'); continue

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
                    except: pass

    zip_buf.seek(0)
    if zip_buf.getbuffer().nbytes <= 22:
        return jsonify({'error': 'All failed', 'details': errors}), 500

    resp = send_file(zip_buf, mimetype='application/zip',
                     as_attachment=True, download_name='ForgeConvert_STL_Export.zip')
    if errors: resp.headers['X-Errors'] = ' | '.join(errors)
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
        return jsonify({'error': 'API key not configured'}), 500

    user_message = data.get('message', '').strip()[:2000]
    history      = data.get('history', [])[-10:]  # last 10 messages only

    # Build messages list
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
                    'for ForgeConvert — a tool that converts STEP, STP, OBJ, IGES files to STL. '
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
    print(f'ForgeConvert v4.0 (GMSH engine) on http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
