import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'vendor'))
import re
import json
import secrets
import hashlib
import qrcode
from io import BytesIO
import base64
from datetime import datetime
from flask import Flask, request, render_template_string, redirect, url_for, session, flash, jsonify
from tinydb import TinyDB, Query
import socket

app = Flask(__name__)

# ============================================
# 获取飞牛环境变量
# ============================================
TRIM_APPDEST = os.environ.get('TRIM_APPDEST', '/app')
TRIM_PKGVAR = os.environ.get('TRIM_PKGVAR', '/app/data')
TRIM_SERVICE_PORT = os.environ.get('TRIM_SERVICE_PORT', '5664')

# ============================================
ACCESSIBLE_PATHS = os.environ.get('TRIM_DATA_ACCESSIBLE_PATHS', '').split(':')
DATA_SHARE_PATH = os.environ.get('TRIM_DATA_SHARE_PATH', '')
if ACCESSIBLE_PATHS and len(ACCESSIBLE_PATHS) > 0 and ACCESSIBLE_PATHS[0] != '':
    UPLOAD_BASE = ACCESSIBLE_PATHS[0]
    print(f"✅ 使用应用设置目录: {UPLOAD_BASE}")
elif DATA_SHARE_PATH:
    UPLOAD_BASE = DATA_SHARE_PATH
    print(f"✅ 使用共享目录: {UPLOAD_BASE}")
else:
    UPLOAD_BASE = os.environ.get('UPLOAD_BASE') or os.environ.get('wizard_upload_path') or os.path.join(TRIM_APPDEST, 'uploads')
    print(f"✅ 使用默认目录: {UPLOAD_BASE}")

DB_FILE = os.environ.get('DB_FILE', os.path.join(TRIM_PKGVAR, 'db.json'))
CONFIG_FILE = os.environ.get('CONFIG_FILE', os.path.join(TRIM_PKGVAR, 'config.json'))
TASKS_FILE = os.environ.get('TASKS_FILE', os.path.join(TRIM_PKGVAR, 'tasks.json'))

# 创建目录
os.makedirs(UPLOAD_BASE, exist_ok=True)
os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)

# ================= 数据库 =================
db = TinyDB(DB_FILE)
tasks_db = TinyDB(TASKS_FILE)

# ================= 辅助函数 =================
def safe_filename(original_filename):
    """保留原始文件名，只过滤危险字符，不加任何前缀"""
    filename = original_filename.split('/')[-1].split('\\')[-1]
    name, ext = os.path.splitext(filename)
    safe_name = re.sub(r'[<>:"/\\|?*]', '_', name).strip()
    if not safe_name:
        safe_name = 'file'
    return f"{safe_name}{ext}"

def get_task_upload_path(task_id, username):
    """按任务ID和提交者姓名分文件夹"""
    safe_username = re.sub(r'[<>:"/\\|?*]', '_', username).strip()
    if not safe_username:
        safe_username = 'anonymous'
    task_dir = os.path.join(UPLOAD_BASE, task_id, safe_username)
    os.makedirs(task_dir, exist_ok=True)
    return task_dir

def get_public_url():
    """获取公网地址：优先从配置文件读取"""
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            return config.get('public_url', '')
    except:
        return ''

def save_public_url(public_url):
    """保存公网地址到配置文件"""
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except:
        config = {}
    
    config['public_url'] = public_url
    
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

# ================= 配置管理 =================
def load_admin_config():
    default_config = {
        'secret_key': secrets.token_hex(16),
        'admin_user': 'admin',
        'admin_password_hash': hashlib.sha256('admin123'.encode()).hexdigest()
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                for k, v in default_config.items():
                    if k not in config:
                        config[k] = v
                return config
        except:
            pass
    return default_config

def save_admin_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

admin_config = load_admin_config()
app.secret_key = admin_config['secret_key']

# ================= 任务管理 =================
def generate_task_id():
    return datetime.now().strftime('%Y%m%d%H%M%S')

def create_task(name, max_size_mb, deadline):
    task_id = generate_task_id()
    task = {
        'id': task_id,
        'name': name,
        'max_size_mb': max_size_mb,
        'deadline': deadline,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'status': 'active',
        'uploads': []
    }
    tasks_db.insert(task)
    return task_id

def get_task(task_id):
    Task = Query()
    result = tasks_db.search(Task.id == task_id)
    return result[0] if result else None

def get_all_tasks():
    return sorted(tasks_db.all(), key=lambda x: x['created_at'], reverse=True)

def delete_task(task_id):
    Task = Query()
    tasks_db.remove(Task.id == task_id)
    import shutil
    task_dir = os.path.join(UPLOAD_BASE, task_id)
    if os.path.exists(task_dir):
        shutil.rmtree(task_dir)

def add_upload_record(task_id, username, filename, size_mb):
    Task = Query()
    task = tasks_db.get(Task.id == task_id)
    if task:
        uploads = task.get('uploads', [])
        uploads.append({
            'username': username,
            'filename': filename,
            'size_mb': size_mb,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
        tasks_db.update({'uploads': uploads}, Task.id == task_id)

def is_task_expired(deadline):
    if not deadline:
        return False
    try:
        return datetime.now() > datetime.strptime(deadline, '%Y-%m-%dT%H:%M')
    except:
        return False

# ================= 前台提交页面模板 =================
FRONT_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>文件收集 - {{ task.name }}</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; }
        .container { background: white; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); width: 100%; max-width: 600px; margin: 0 auto; padding: 2rem; }
        h1 { color: #333; margin-bottom: 0.5rem; text-align: center; }
        .task-name { text-align: center; color: #667eea; margin-bottom: 1.5rem; font-size: 0.9rem; }
        .deadline { text-align: center; padding: 8px; border-radius: 8px; margin-bottom: 1rem; font-size: 0.85rem; background: #fff3cd; color: #856404; }
        .guide { background: #e8f4fd; border-left: 4px solid #2196F3; padding: 12px 16px; margin-bottom: 24px; border-radius: 8px; font-size: 15px; line-height: 1.6; }
        .guide strong { color: #1976D2; }
        .form-group { margin-bottom: 1.5rem; }
        label { display: block; margin-bottom: 0.5rem; font-weight: bold; color: #555; }
        input[type="text"] { width: 100%; padding: 12px; border: 2px solid #e0e0e0; border-radius: 8px; font-size: 1rem; }
        input[type="text"]:focus { outline: none; border-color: #667eea; }
        .file-area { border: 2px dashed #667eea; border-radius: 12px; padding: 30px; text-align: center; cursor: pointer; background: #f8f9ff; transition: all 0.3s; }
        .file-area:hover { background: #f0f2ff; border-color: #764ba2; }
        .file-area.dragover { background: #e8ebff; }
        .file-area input { display: none; }
        .file-label { color: #667eea; font-weight: bold; cursor: pointer; }
        .file-label span { font-size: 2rem; display: block; margin-bottom: 8px; }
        #file-names { margin-top: 12px; font-size: 0.85rem; color: #666; word-break: break-all; max-height: 80px; overflow-y: auto; }
        .progress-container { margin-top: 15px; display: none; }
        .progress-bar-wrapper { width: 100%; height: 8px; background: #e0e0e0; border-radius: 4px; overflow: hidden; }
        .progress-bar { width: 0%; height: 100%; background: linear-gradient(90deg, #667eea, #764ba2); transition: width 0.3s; }
        .progress-text { text-align: center; font-size: 0.8rem; color: #667eea; margin-top: 5px; }
        button { width: 100%; padding: 14px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; border-radius: 8px; font-size: 1rem; font-weight: bold; cursor: pointer; transition: transform 0.2s; }
        button:hover { transform: translateY(-2px); }
        button:disabled { opacity: 0.6; transform: none; cursor: not-allowed; }
        .alert { padding: 12px; border-radius: 8px; margin-bottom: 20px; }
        .alert.success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .alert.error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .footer { margin-top: 2rem; text-align: center; font-size: 0.75rem; color: #999; border-top: 1px solid #eee; padding-top: 1.5rem; }
        .footer a { color: #667eea; text-decoration: none; }
        @media (max-width: 600px) { .container { padding: 1.5rem; } }
    </style>
</head>
<body>
    <div class="container">
        <h1>📂 文件收集助手v1.0.0</h1>
        <div class="task-name">📋 {{ task.name }}</div>
        {% if task.deadline %}
            <div class="deadline">⏰ 截止时间：{{ task.deadline.replace('T', ' ') }}</div>
        {% endif %}
        <div class="guide">
            <strong>📖 使用说明</strong><br>
            • 填写您的姓名后，点击下方区域选择要提交的文件<br>
            • 支持一次选择多个文件<br>
            • 单文件大小上限：{{ task.max_size_mb }} MB<br>
            • 提交成功后，文件将自动上传，请等待完成提示
        </div>
        <div id="alert-container"></div>
        <form id="upload-form" enctype="multipart/form-data">
            <div class="form-group">
                <label>👤 请输入您的姓名/代号</label>
                <input type="text" id="username" name="username" required placeholder="例如：张三">
            </div>
            <div class="form-group">
                <label>📎 选择文件（支持多选）</label>
                <div class="file-area" id="file-area">
                    <div class="file-label">
                        <span>📁</span><br>点击或拖拽文件到此区域
                    </div>
                    <input type="file" id="file-input" name="files" multiple>
                </div>
                <div id="file-names"></div>
            </div>
            <div class="progress-container" id="progress-container">
                <div class="progress-bar-wrapper">
                    <div class="progress-bar" id="progress-bar"></div>
                </div>
                <div class="progress-text" id="progress-text">正在上传...</div>
            </div>
            <button type="submit" id="submit-btn">🚀 提交上传</button>
        </form>
        <div class="footer">
            <p>© 2026 文件收集系统 | 开发者：晦华先生</p>
        </div>
    </div>
    <script>
        const fileInput = document.getElementById('file-input');
        const fileArea = document.getElementById('file-area');
        const fileNamesDiv = document.getElementById('file-names');
        const progressContainer = document.getElementById('progress-container');
        const progressBar = document.getElementById('progress-bar');
        const progressText = document.getElementById('progress-text');
        const submitBtn = document.getElementById('submit-btn');
        const usernameInput = document.getElementById('username');
        const alertContainer = document.getElementById('alert-container');
        function showAlert(message, type) {
            const alertDiv = document.createElement('div');
            alertDiv.className = `alert ${type}`;
            alertDiv.innerHTML = message;
            alertContainer.appendChild(alertDiv);
            setTimeout(() => alertDiv.remove(), 5000);
        }
        function updateFileNames() {
            if (fileInput.files.length > 0) {
                let names = Array.from(fileInput.files).map(f => f.name).join('、');
                if (names.length > 80) names = names.substring(0, 80) + '...';
                fileNamesDiv.innerHTML = `<strong>已选择 ${fileInput.files.length} 个文件：</strong><br>${names}`;
            } else {
                fileNamesDiv.innerHTML = '';
            }
        }
        fileInput.addEventListener('change', updateFileNames);
        fileArea.addEventListener('dragover', (e) => { e.preventDefault(); fileArea.classList.add('dragover'); });
        fileArea.addEventListener('dragleave', () => { fileArea.classList.remove('dragover'); });
        fileArea.addEventListener('drop', (e) => {
            e.preventDefault();
            fileArea.classList.remove('dragover');
            fileInput.files = e.dataTransfer.files;
            updateFileNames();
        });
        fileArea.addEventListener('click', () => { fileInput.click(); });
        document.getElementById('upload-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const username = usernameInput.value.trim();
            if (!username) { showAlert('请填写您的姓名', 'error'); return; }
            if (fileInput.files.length === 0) { showAlert('请选择要上传的文件', 'error'); return; }
            const formData = new FormData();
            formData.append('username', username);
            for (let i = 0; i < fileInput.files.length; i++) {
                formData.append('files', fileInput.files[i]);
            }
            submitBtn.disabled = true;
            submitBtn.textContent = '上传中...';
            progressContainer.style.display = 'block';
            progressBar.style.width = '0%';
            progressText.textContent = '正在上传 0%';
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/upload/{{ task.id }}', true);
            xhr.upload.addEventListener('progress', (e) => {
                if (e.lengthComputable) {
                    const percent = Math.round((e.loaded / e.total) * 100);
                    progressBar.style.width = percent + '%';
                    progressText.textContent = `正在上传 ${percent}%`;
                }
            });
            xhr.onload = () => {
                submitBtn.disabled = false;
                submitBtn.textContent = '🚀 提交上传';
                progressContainer.style.display = 'none';
                if (xhr.status === 200) {
                    const result = JSON.parse(xhr.responseText);
                    if (result.success) {
                        showAlert(result.message, 'success');
                        fileInput.value = '';
                        updateFileNames();
                        usernameInput.value = '';
                    } else {
                        showAlert(result.message, 'error');
                    }
                } else {
                    showAlert('上传失败，请重试', 'error');
                }
            };
            xhr.onerror = () => {
                submitBtn.disabled = false;
                submitBtn.textContent = '🚀 提交上传';
                progressContainer.style.display = 'none';
                showAlert('网络错误，请重试', 'error');
            };
            xhr.send(formData);
        });
    </script>
</body>
</html>
"""

# ================= 后台管理模板 =================
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理后台 - 文件收集系统</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f7fa; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { background: white; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
        .header h1 { color: #333; font-size: 1.5rem; }
        .header a { color: #667eea; text-decoration: none; margin-left: 15px; }
        .card { background: white; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        .card h2 { margin-bottom: 20px; color: #333; font-size: 1.2rem; border-bottom: 2px solid #667eea; padding-bottom: 10px; display: inline-block; }
        .form-row { display: flex; gap: 15px; align-items: flex-end; flex-wrap: wrap; margin-bottom: 15px; }
        .form-group { display: flex; flex-direction: column; gap: 5px; }
        .form-group label { font-weight: bold; color: #555; font-size: 0.85rem; }
        .form-group input { padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; }
        .form-group input[type="text"] { width: 100%; min-width: 250px; }
        .form-group input[type="number"] { width: 100px; }
        .form-group input[type="datetime-local"] { width: 180px; }
        button { padding: 8px 20px; background: #667eea; color: white; border: none; border-radius: 6px; cursor: pointer; }
        button:hover { background: #5a67d8; }
        button.delete { background: #e53e3e; }
        button.delete:hover { background: #c53030; }
        button.small { padding: 4px 12px; font-size: 0.8rem; }
        button.success { background: #28a745; }
        button.success:hover { background: #218838; }
        .alert { padding: 12px; border-radius: 8px; margin-bottom: 20px; }
        .alert.success { background: #d4edda; color: #155724; }
        .alert.error { background: #f8d7da; color: #721c24; }
        .alert.warning { background: #fff3cd; color: #856404; }
        .info-text { font-size: 12px; color: #999; margin-top: 5px; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 0.85rem; }
        th { background: #f8f9fa; font-weight: bold; color: #555; }
        tr:hover { background: #f8f9fa; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7rem; }
        .badge-active { background: #d4edda; color: #155724; }
        @media (max-width: 768px) { th, td { font-size: 0.7rem; padding: 6px; } .form-row { flex-direction: column; align-items: stretch; } .form-group input { width: 100% !important; } }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🛠️ 文件收集助手 - 管理后台</h1>
            <div><a href="/admin/login">🏠 登录页</a> <a href="/logout">🚪 退出登录</a></div>
        </div>
        
        <div id="alert-container">
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% for category, message in messages %}
            <div class="alert {{ category }}">{{ message }}</div>
          {% endfor %}
        {% endwith %}
        </div>
        
        <!-- 公网地址设置 -->
        <div class="card">
            <h2>🌐 公网收集地址设置</h2>
            <div class="form-row">
                <div class="form-group" style="flex:2;">
                    <label>公网收集地址</label>
                    <input type="text" id="public_url_input" value="{{ public_url }}" placeholder="例如：https://yourdomain.com">
                    <div class="info-text">留空则自动使用当前访问地址。修改后立即生效，无需重启容器。</div>
                </div>
                <button id="save_public_url_btn" class="success">保存</button>
            </div>
        </div>
        
        <!-- 新建收集任务 -->
        <div class="card">
            <h2>📋 新建收集任务</h2>
            <form method="post" action="/admin/task/create">
                <div class="form-row">
                    <div class="form-group">
                        <label>收集任务名称</label>
                        <input type="text" name="task_name" placeholder="例如：2026年Q2工作总结" required>
                    </div>
                    <div class="form-group">
                        <label>单文件大小限制 (MB)</label>
                        <input type="number" name="max_size_mb" value="50" min="1" max="1024" required>
                    </div>
                    <div class="form-group">
                        <label>任务截止时间</label>
                        <input type="datetime-local" name="deadline">
                    </div>
                    <button type="submit">创建任务</button>
                </div>
            </form>
        </div>
        
        <!-- 已有任务列表 -->
        <div class="card">
            <h2>📁 已有收集任务</h2>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr><th>任务名称</th><th>任务ID</th><th>大小限制</th><th>截止时间</th><th>状态</th><th>收集链接</th><th>操作</th></tr>
                    </thead>
                    <tbody>
                        {% for task in tasks %}
                        <tr>
                            <td>{{ task.name }}</td>
                            <td><code>{{ task.id }}</code></td>
                            <td>{{ task.max_size_mb }} MB</td>
                            <td>{{ task.deadline.replace('T', ' ') if task.deadline else '无' }}</td>
                            <td><span class="badge badge-active">进行中</span></td>
                            <td style="max-width: 300px;">
                                <div style="display: flex; gap: 5px; flex-wrap: wrap;">
                                    <input type="text" id="link-{{ task.id }}" value="{{ base_url }}/fc/{{ task.id }}" readonly style="flex:1; min-width: 150px;">
                                    <button class="small" onclick="copyLink('{{ task.id }}')">复制</button>
                                    <button class="small" onclick="showQR('{{ task.id }}', '{{ base_url }}/fc/{{ task.id }}', '{{ task.name }}')">二维码</button>
                                </div>
                                <div id="qr-{{ task.id }}" style="display:none;"></div>
                            </td>
                            <td>
                                <form method="post" action="/admin/task/delete/{{ task.id }}" style="display:inline;" onsubmit="return confirm('确定删除任务及所有文件吗？')">
                                    <button type="submit" class="delete small">删除</button>
                                </form>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            {% if not tasks %}
            <p style="text-align: center; color: #999; padding: 40px;">暂无收集任务，请先创建</p>
            {% endif %}
        </div>
        
        <!-- 修改密码 -->
        <div class="card">
            <h2>🔑 修改管理员密码</h2>
            <form method="post" action="/admin/password">
                <div class="form-row">
                    <div class="form-group"><label>新密码</label><input type="password" name="new_password" required></div>
                    <button type="submit">修改</button>
                </div>
            </form>
        </div>
    </div>
    
    <script>
        function copyLink(taskId) {
            const input = document.getElementById('link-' + taskId);
            input.select();
            document.execCommand('copy');
            alert('链接已复制到剪贴板');
        }
        
        let currentQRImage = null;
        let currentTaskName = '';
        
        async function showQR(taskId, url, taskName) {
            let modal = document.getElementById('qr-modal');
            if (!modal) {
                modal = document.createElement('div');
                modal.id = 'qr-modal';
                modal.style.cssText = `
                    display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                    background: rgba(0,0,0,0.5); z-index: 9999; justify-content: center; align-items: center;
                `;
                modal.onclick = (e) => { if (e.target === modal) modal.style.display = 'none'; };
                document.body.appendChild(modal);
            }
            
            modal.innerHTML = `
                <div style="background: white; border-radius: 16px; padding: 24px; width: 320px; text-align: center; box-shadow: 0 20px 60px rgba(0,0,0,0.3);">
                    <div style="margin-bottom: 15px;">
                        <strong style="font-size: 18px; color: #333;">📱 收集任务二维码</strong>
                    </div>
                    <div style="margin-bottom: 10px; padding: 8px; background: #f0f2ff; border-radius: 8px;">
                        <div style="font-size: 14px; color: #667eea; font-weight: bold;">${escapeHtml(taskName)}</div>
                        <div style="font-size: 11px; color: #999; margin-top: 4px;">扫码或长按识别进入提交页面</div>
                    </div>
                    <div id="qr-code-container" style="display: flex; justify-content: center; margin: 15px 0;">
                        <div style="text-align:center">⏳ 生成中...</div>
                    </div>
                    <div style="margin-bottom: 15px;">
                        <div style="font-size: 11px; color: #999; word-break: break-all;">${escapeHtml(url)}</div>
                    </div>
                    <div style="display: flex; gap: 12px;">
                        <button id="save-qr-btn" style="flex:1; padding: 8px; background: #667eea; color: white; border: none; border-radius: 6px; cursor: pointer;">💾 保存图片</button>
                        <button id="copy-link-btn" style="flex:1; padding: 8px; background: #28a745; color: white; border: none; border-radius: 6px; cursor: pointer;">📋 复制链接</button>
                    </div>
                    <div style="margin-top: 12px;">
                        <button id="close-qr-btn" style="padding: 6px 20px; background: #6c757d; color: white; border: none; border-radius: 6px; cursor: pointer;">关闭</button>
                    </div>
                </div>
            `;
            
            modal.style.display = 'flex';
            
            document.getElementById('save-qr-btn').onclick = () => downloadQR();
            document.getElementById('copy-link-btn').onclick = () => copyLinkAndClose(taskId);
            document.getElementById('close-qr-btn').onclick = () => closeQRModal();
            
            try {
                const response = await fetch('/qr?url=' + encodeURIComponent(url));
                const data = await response.json();
                if (data.qr_image) {
                    const qrContainer = document.getElementById('qr-code-container');
                    qrContainer.innerHTML = `<img src="${data.qr_image}" style="width:180px;height:180px;display:block;margin:0 auto;">`;
                    currentQRImage = data.qr_image;
                    currentTaskName = taskName;
                } else {
                    document.getElementById('qr-code-container').innerHTML = '<div style="color:red">❌ 生成失败</div>';
                }
            } catch (error) {
                document.getElementById('qr-code-container').innerHTML = '<div style="color:red">❌ 生成失败</div>';
            }
        }
        
        function closeQRModal() {
            const modal = document.getElementById('qr-modal');
            if (modal) modal.style.display = 'none';
        }
        
        function downloadQR() {
            if (currentQRImage) {
                const link = document.createElement('a');
                link.download = `${currentTaskName}_二维码.png`;
                link.href = currentQRImage;
                link.click();
            } else {
                alert('二维码尚未生成，请稍后');
            }
        }
        
        function copyLinkAndClose(taskId) {
            const input = document.getElementById('link-' + taskId);
            input.select();
            document.execCommand('copy');
            alert('链接已复制到剪贴板');
            closeQRModal();
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        // 保存公网地址
        document.getElementById('save_public_url_btn').addEventListener('click', async () => {
            const publicUrl = document.getElementById('public_url_input').value.trim();
            const btn = document.getElementById('save_public_url_btn');
            btn.disabled = true;
            btn.textContent = '保存中...';
            
            try {
                const response = await fetch('/admin/save_public_url', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ public_url: publicUrl })
                });
                const result = await response.json();
                if (result.success) {
                    alert('公网地址保存成功，页面将刷新');
                    location.reload();
                } else {
                    alert('保存失败：' + (result.message || '未知错误'));
                }
            } catch (error) {
                alert('保存失败：网络错误');
            } finally {
                btn.disabled = false;
                btn.textContent = '保存';
            }
        });
    </script>
</body>
</html>
"""

# ================= 登录模板 =================
LOGIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>管理员登录</title>
    <style>
        body { display: flex; justify-content: center; align-items: center; min-height: 100vh; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); font-family: sans-serif; }
        .box { background: white; padding: 40px; border-radius: 16px; width: 380px; text-align: center; box-shadow: 0 20px 60px rgba(0,0,0,0.3); }
        h2 { margin-bottom: 20px; color: #333; }
        input { width: 100%; padding: 12px; margin: 10px 0; border: 2px solid #e0e0e0; border-radius: 8px; box-sizing: border-box; font-size: 1rem; }
        input:focus { outline: none; border-color: #667eea; }
        button { width: 100%; padding: 12px; background: linear-gradient(135deg, #667eea, #764ba2); color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 1rem; }
        button:hover { opacity: 0.9; }
        .error { color: #e53e3e; margin-bottom: 15px; font-size: 0.9rem; }
        .admin-tips { background: #e8f4fd; border-left: 4px solid #2196F3; padding: 12px; margin-bottom: 20px; border-radius: 8px; font-size: 15px; text-align: left; line-height: 1.5; }
        .admin-tips strong { color: #1976D2; }
        a { color: #667eea; text-decoration: none; display: block; margin-top: 20px; font-size: 0.9rem; }
    </style>
</head>
<body>
    <div class="box">
        <h2>🔐 管理员登录</h2>
        
        <div class="admin-tips">
            <strong>📌 管理员操作指引</strong><br>
            • 登录后台,填写任务信息,即可创建/管理收集任务<br>
            • 任务创建后会自动生成收集地址链接,分享给文件提交者即可开始收集<br>
            • 若在局域网内新建公网收集任务,请设置好反代/穿透地址之后,进入后台设置公网收集地址<br>
            • 管理员账号:admin,初始密码:admin123<br>
            • 强烈建议首次登录后台立即修改管理员密码。                      
        </div>
        
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="post">
            <input type="text" name="username" placeholder="用户名" required>
            <input type="password" name="password" placeholder="密码" required>
            <button type="submit">登录</button>
        </form>
        <a href="/admin/login">刷新页面</a>
    </div>
</body>
</html>
"""

# ================= 路由 =================

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/fc/<task_id>', methods=['GET'])
def front_page(task_id):
    task = get_task(task_id)
    if not task:
        return "任务不存在或已删除", 404
    if is_task_expired(task.get('deadline', '')):
        return "该收集任务已过期", 403
    return render_template_string(FRONT_HTML, task=task)

@app.route('/upload/<task_id>', methods=['POST'])
def upload_file(task_id):
    task = get_task(task_id)
    if not task:
        return jsonify({'success': False, 'message': '任务不存在'})
    if is_task_expired(task.get('deadline', '')):
        return jsonify({'success': False, 'message': '任务已过期'})
    
    username = request.form.get('username', '匿名').strip()
    if not username:
        username = '匿名'
    
    files = request.files.getlist('files')
    max_size_bytes = task.get('max_size_mb', 50) * 1024 * 1024
    
    uploaded_count = 0
    for file in files:
        if not file or file.filename == '':
            continue
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(0)
        if size > max_size_bytes:
            continue
        
        # 安全的原始文件名（不加任何前缀）
        safe_name = safe_filename(file.filename)
        
        # 用户专属目录：任务ID/提交者名/
        user_dir = get_task_upload_path(task_id, username)
        save_path = os.path.join(user_dir, safe_name)
        
        # 防重名：同一用户上传同名文件时加数字
        name_without_ext, ext = os.path.splitext(safe_name)
        counter = 1
        while os.path.exists(save_path):
            new_name = f"{name_without_ext}_{counter}{ext}"
            save_path = os.path.join(user_dir, new_name)
            counter += 1
            if counter > 100:
                break
        
        try:
            file.save(save_path)
            add_upload_record(task_id, username, safe_name, round(size / 1024 / 1024, 2))
            uploaded_count += 1
        except Exception as e:
            print(f"保存失败: {e}")
    
    if uploaded_count > 0:
        return jsonify({'success': True, 'message': f'成功上传 {uploaded_count} 个文件'})
    else:
        return jsonify({'success': False, 'message': '上传失败，请检查文件大小限制'})

@app.route('/qr')
def generate_qr():
    url = request.args.get('url', '')
    if not url:
        return jsonify({'error': '缺少 url 参数'}), 400
    
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=4,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_base64 = base64.b64encode(buffered.getvalue()).decode()
        
        return jsonify({'qr_image': f'data:image/png;base64,{img_base64}'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == admin_config.get('admin_user') and hashlib.sha256(password.encode()).hexdigest() == admin_config.get('admin_password_hash'):
            session['is_admin'] = True
            return redirect(url_for('admin'))
        return render_template_string(LOGIN_HTML, error="账号或密码错误")
    return render_template_string(LOGIN_HTML)

@app.route('/admin')
def admin():
    if not session.get('is_admin'):
        return redirect(url_for('login'))
    tasks = get_all_tasks()
    
    # 获取公网地址（从配置文件）
    public_url = get_public_url()
    
    # 生成 base_url
    if public_url:
        base_url = public_url.rstrip('/')
    else:
        scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
        base_url = f"{scheme}://{request.host}"
    
    return render_template_string(ADMIN_HTML, tasks=tasks, base_url=base_url, public_url=public_url)

@app.route('/admin/save_public_url', methods=['POST'])
def save_public_url_route():
    if not session.get('is_admin'):
        return jsonify({'success': False, 'message': '未登录'}), 401
    
    data = request.get_json()
    public_url = data.get('public_url', '').strip()
    
    save_public_url(public_url)
    
    return jsonify({'success': True, 'message': '保存成功'})

@app.route('/admin/task/create', methods=['POST'])
def create_task_route():
    if not session.get('is_admin'):
        return redirect(url_for('login'))
    name = request.form.get('task_name', '').strip()
    max_size_mb = int(request.form.get('max_size_mb', 50))
    deadline = request.form.get('deadline', '')
    if not name:
        flash('请输入任务名称', 'error')
        return redirect(url_for('admin'))
    task_id = create_task(name, max_size_mb, deadline)
    flash(f'任务 "{name}" 创建成功', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/task/delete/<task_id>', methods=['POST'])
def delete_task_route(task_id):
    if not session.get('is_admin'):
        return redirect(url_for('login'))
    task = get_task(task_id)
    if task:
        delete_task(task_id)
        flash(f'任务 "{task["name"]}" 已删除', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/password', methods=['POST'])
def update_password():
    if not session.get('is_admin'):
        return redirect(url_for('login'))
    new_pwd = request.form.get('new_password')
    if new_pwd and len(new_pwd) >= 4:
        admin_config['admin_password_hash'] = hashlib.sha256(new_pwd.encode()).hexdigest()
        save_admin_config(admin_config)
        flash('密码修改成功', 'success')
    else:
        flash('密码长度至少4位', 'error')
    return redirect(url_for('admin'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    port = int(os.environ.get('port') or os.environ.get('wizard_port') or os.environ.get('TRIM_SERVICE_PORT', 5664))
    app.run(host='0.0.0.0', port=port, debug=False)