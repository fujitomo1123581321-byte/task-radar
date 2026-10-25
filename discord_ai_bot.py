"""
Discord AI Task Bot for Tokyo University of Science (TUS)
Powered by Google Gemini (Auto-fallback: 3.8 -> 3.7 -> 3.6 -> 3.5 -> 2.5) & discord.py
Features:
- 24/7 Cloud Ready (Embedded aiohttp health check for Render/Koyeb)
- Interactive Dialog & Confirmation UI (Discord Buttons)
- Simple & Robust 1-Deadline + Natural Memo Architecture
- Cloud Git Sync via GitHub API (with local git push fallback)
"""

import os
import sys

# Ensure UTF-8 output on Windows console
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import json
import re
import time
import base64
import asyncio
import subprocess
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

import threading
import http.server
import discord
from discord.ext import commands
from aiohttp import web

# -------------------------------------------------------------
# Configuration & Paths (Local + Cloud Support)
# -------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
JSON_PATH = os.path.join(BASE_DIR, "assignments.json")
JS_PATH = os.path.join(BASE_DIR, "assignments.js")
IGNORED_PATH = os.path.join(BASE_DIR, "ignored_titles.json")

JST = timezone(timedelta(hours=9))

def load_ignored_titles():
    if os.path.exists(IGNORED_PATH):
        try:
            with open(IGNORED_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []

def save_ignored_titles(titles):
    try:
        with open(IGNORED_PATH, "w", encoding="utf-8") as f:
            json.dump(list(set(titles)), f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass

    # Cloud environment variable fallbacks
    if not cfg.get("discord_bot_token"):
        cfg["discord_bot_token"] = os.environ.get("DISCORD_BOT_TOKEN")
    if not cfg.get("gemini_api_key"):
        cfg["gemini_api_key"] = os.environ.get("GEMINI_API_KEY")
    if not cfg.get("github_token"):
        cfg["github_token"] = os.environ.get("GITHUB_TOKEN")
    if not cfg.get("letus_calendar_url"):
        cfg["letus_calendar_url"] = os.environ.get("LETUS_CALENDAR_URL")

    url_file = os.path.join(BASE_DIR, "letus_url.txt")
    if os.path.exists(url_file):
        try:
            with open(url_file, "r", encoding="utf-8") as f:
                saved = f.read().strip()
                if saved:
                    cfg["letus_calendar_url"] = saved
        except Exception:
            pass

    return cfg

CONFIG = load_config()

DATA_LOCK = threading.RLock()

# Debounced git push management
_git_push_timer = None
_pending_commit_msgs = []
_git_push_timer_lock = threading.Lock()

def fetch_remote_assignments():
    try:
        url = f"https://raw.githubusercontent.com/fujitomo1123581321-byte/task-radar/main/assignments.json?_={int(time.time())}"
        req = urllib.request.Request(url, headers={"User-Agent": "TaskRadarBot"})
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.loads(res.read().decode("utf-8"))
    except Exception:
        return []

def load_assignments(force_remote: bool = False):
    with DATA_LOCK:
        local = []
        if os.path.exists(JSON_PATH):
            try:
                with open(JSON_PATH, "r", encoding="utf-8") as f:
                    local = json.load(f)
            except Exception:
                local = []

        # Local is authoritative during normal runtime to prevent stale CDN overwrites and eliminate read latency
        if local and not force_remote:
            return local

        # Only fetch remote if local is empty/missing or explicitly forced (e.g., initial startup)
        remote = fetch_remote_assignments()
        if not remote:
            return local
        if not local:
            _raw_save_assignments(remote)
            return remote

        # 2-way Smart Merge: Remote (GitHub Actions/LETUS updates) + Local (user changes)
        local_map = {str(a.get("id")): a for a in local if a.get("id")}
        merged = []
        for r in remote:
            rid = str(r.get("id"))
            if rid in local_map:
                loc = local_map.pop(rid)
                if loc.get("status") == "submitted":
                    r["status"] = "submitted"
                    r["completedAt"] = loc.get("completedAt") or r.get("completedAt")
                elif loc.get("status") == "in_progress" and r.get("status") == "not_started":
                    r["status"] = "in_progress"
                merged.append(r)
            else:
                merged.append(r)
        for loc_item in local_map.values():
            merged.append(loc_item)

        _raw_save_assignments(merged)
        return merged

def _raw_save_assignments(items):
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    with open(JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.INITIAL_ASSIGNMENTS = " + json.dumps(items, ensure_ascii=False, indent=2) + ";\n")

def save_assignments(items):
    with DATA_LOCK:
        _raw_save_assignments(items)

def sync_to_github_via_api(path: str, content_bytes: bytes, commit_msg: str, token: str, repo: str = "fujitomo1123581321-byte/task-radar"):
    """Update file in GitHub repo directly using GitHub REST API (No git CLI required in cloud)"""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "TaskRadarCloudBot",
        "Accept": "application/vnd.github.v3+json"
    }
    for attempt in range(3):
        sha = None
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as res:
                data = json.loads(res.read().decode("utf-8"))
                sha = data.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"[GitHub API GET Warning] {path} HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:100]}")
        except Exception as e:
            print(f"[GitHub API GET Warning] {path} error: {e}")

        payload = {
            "message": commit_msg,
            "content": base64.b64encode(content_bytes).decode("utf-8")
        }
        if sha:
            payload["sha"] = sha

        put_req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="PUT"
        )
        try:
            with urllib.request.urlopen(put_req, timeout=10) as res:
                return res.status in (200, 201)
        except urllib.error.HTTPError as e:
            print(f"[GitHub API PUT Error] {path} HTTP {e.code} (attempt {attempt+1}): {e.read().decode('utf-8', errors='ignore')[:100]}")
            if e.code == 409 and attempt < 2:
                time.sleep(1.0)
                continue
            return False
        except Exception as e:
            print(f"[GitHub API PUT Error] {path} error: {e}")
            return False
    return False

def git_push_sync(commit_msg="Update assignments via Discord Bot"):
    """Dual-mode sync: GitHub API (Cloud) or Git Subprocess (Local)"""
    with DATA_LOCK:
        gh_token = CONFIG.get("github_token") or os.environ.get("GITHUB_TOKEN")
        
        if gh_token:
            try:
                with open(JSON_PATH, "rb") as f:
                    json_bytes = f.read()
                with open(JS_PATH, "rb") as f:
                    js_bytes = f.read()

                sync_to_github_via_api("assignments.json", json_bytes, commit_msg, gh_token)
                time.sleep(0.5)
                sync_to_github_via_api("assignments.js", js_bytes, commit_msg, gh_token)

                if os.path.exists(IGNORED_PATH):
                    with open(IGNORED_PATH, "rb") as f:
                        ign_bytes = f.read()
                    time.sleep(0.5)
                    sync_to_github_via_api("ignored_titles.json", ign_bytes, commit_msg, gh_token)

                print(f"[Cloud Git Sync] Pushed to GitHub via API: {commit_msg}")
                return True
            except Exception as e:
                print(f"[Cloud Git Sync Warning] API sync failed, trying local git: {e}")

        try:
            files = ["assignments.json", "assignments.js"]
            if os.path.exists(IGNORED_PATH):
                files.append("ignored_titles.json")
            subprocess.run(["git", "add"] + files, cwd=BASE_DIR, check=True, capture_output=True)
            status = subprocess.run(["git", "status", "--porcelain"] + files, cwd=BASE_DIR, capture_output=True, text=True)
            if any(f in status.stdout for f in files):
                subprocess.run(["git", "commit", "-m", commit_msg], cwd=BASE_DIR, check=True, capture_output=True)
                subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR, check=True, capture_output=True)
                print(f"[Local Git Sync] Successfully pushed to GitHub: {commit_msg}")
                return True
            return False
        except Exception as e:
            print(f"[Git Sync Notice] {e}")
            return False

def _execute_debounced_push():
    global _git_push_timer, _pending_commit_msgs
    with _git_push_timer_lock:
        _git_push_timer = None
        if not _pending_commit_msgs:
            return
        msgs = list(dict.fromkeys([m for m in _pending_commit_msgs if m]))
        _pending_commit_msgs = []

    combined_msg = " ; ".join(msgs)
    if len(combined_msg) > 130:
        combined_msg = f"Batch update ({len(msgs)} actions): {msgs[0][:40]}..."
    print(f"[Debounced Git Sync] Starting bundled push to GitHub: {combined_msg}")
    threading.Thread(target=git_push_sync, args=(combined_msg,), daemon=True).start()

def schedule_git_push(commit_msg: str, delay: float = 60.0):
    """Debounce multiple rapid actions into a single GitHub commit after delay seconds"""
    global _git_push_timer, _pending_commit_msgs
    with _git_push_timer_lock:
        if commit_msg:
            _pending_commit_msgs.append(commit_msg)
        if _git_push_timer is not None:
            try:
                _git_push_timer.cancel()
            except Exception:
                pass
        _git_push_timer = threading.Timer(delay, _execute_debounced_push)
        _git_push_timer.daemon = True
        _git_push_timer.start()
        print(f"[Debounced Git Sync] Queued push in {delay}s: {commit_msg}")

# -------------------------------------------------------------
# Session & Conversation History Management
# -------------------------------------------------------------
user_sessions = {}
user_chat_histories = {}
SESSION_TIMEOUT = 600  # 10分間有効

def get_user_session(user_id: int):
    now = time.time()
    if user_id in user_sessions:
        session = user_sessions[user_id]
        if now - session.get("updated_at", 0) < SESSION_TIMEOUT:
            session["updated_at"] = now
            return session
        else:
            del user_sessions[user_id]
    return None

def clear_user_session(user_id: int):
    if user_id in user_sessions:
        del user_sessions[user_id]

def add_chat_history(user_id: int, role: str, content: str):
    """
    ユーザーとBotの直近のやり取り（直近10分・最大8件）をインメモリに保持
    role: "user" | "assistant"
    """
    now = time.time()
    if user_id not in user_chat_histories:
        user_chat_histories[user_id] = []
    # 有効期限切れ（10分以上経過）をパージ
    user_chat_histories[user_id] = [
        item for item in user_chat_histories[user_id]
        if now - item.get("time", 0) < SESSION_TIMEOUT
    ]
    user_chat_histories[user_id].append({
        "role": role,
        "content": str(content)[:600],
        "time": now
    })
    # 最大8件（4往復）に制限
    if len(user_chat_histories[user_id]) > 8:
        user_chat_histories[user_id] = user_chat_histories[user_id][-8:]

def get_chat_history(user_id: int) -> list:
    now = time.time()
    if user_id not in user_chat_histories:
        return []
    valid = [
        item for item in user_chat_histories[user_id]
        if now - item.get("time", 0) < SESSION_TIMEOUT
    ]
    user_chat_histories[user_id] = valid
    return valid

def clear_chat_history(user_id: int):
    if user_id in user_chat_histories:
        del user_chat_histories[user_id]

# -------------------------------------------------------------
# Gemini AI Engine with Dynamic Fallback
# -------------------------------------------------------------
GEMINI_MODELS = [
    "gemini-flash-lite-latest",  # 常に最新の超高速Flash-Lite（最優先・即答特化）
    "gemini-3.5-flash-lite",     # 最新3.5世代の超高速Flash-Lite ⚡️
    "gemini-3.1-flash-lite",     # 3.1 Flash-Lite
    "gemini-2.5-flash-lite",     # 安定版2.5 Flash-Lite
    "gemini-flash-latest",       # 最新Flash標準
    "gemini-2.5-flash"           # 2.5 Flash標準
]

def call_gemini(prompt: str, api_key: str):
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2048
        }
    }).encode("utf-8")

    last_error = None
    for model_name in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        timeout = 5 if "lite" in model_name else 8
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                text = res_data["candidates"][0]["content"]["parts"][0]["text"]
                return text, model_name
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            print(f"[Gemini Fallback] Model {model_name} HTTP {e.code}: {err_body[:80]}")
            last_error = f"{model_name} (HTTP {e.code})"
            continue
        except Exception as e:
            print(f"[Gemini Fallback] Model {model_name} Error: {e}")
            last_error = f"{model_name} ({e})"
            continue

    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")

def parse_json_from_ai(ai_text: str):
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", ai_text)
    raw = m.group(1).strip() if m else ai_text.strip()
    try:
        return json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw[start:end+1])
        raise ValueError("Could not parse JSON from AI output.")

# -------------------------------------------------------------
# Dialog Flow Logic (Clean 1-Deadline + Natural Memo Architecture)
# -------------------------------------------------------------
def analyze_dialog_with_gemini(user_id: int, user_msg: str, api_key: str, prev_history: list = None):
    assignments = load_assignments()
    now_dt = datetime.now(JST)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S (JST, %A)")

    # 今週末の日曜日 23:59:00 の計算 (JST)
    days_until_sunday = (6 - now_dt.weekday()) % 7
    if days_until_sunday == 0 and now_dt.hour >= 20:
        days_until_sunday = 7
    default_sunday_dt = (now_dt + timedelta(days=days_until_sunday)).replace(hour=23, minute=59, second=0, microsecond=0)
    default_deadline_iso = default_sunday_dt.strftime("%Y-%m-%dT23:59:00")
    default_deadline_display = default_sunday_dt.strftime("%Y年%m月%d日(%a) 23:59")

    session = get_user_session(user_id)
    existing_draft = session.get("draft") if session else None

    if prev_history is None:
        prev_history = get_chat_history(user_id)

    if prev_history:
        history_lines = []
        for h in prev_history:
            spk = "ユーザー" if h.get("role") == "user" else "アシスタント"
            history_lines.append(f"- {spk}: {h.get('content')}")
        history_text = "\n".join(history_lines)
    else:
        history_text = "なし（前回の会話なし）"

    uncompleted = [a for a in assignments if a.get("status") != "submitted"]
    tasks_summary = []
    for t in uncompleted:
        tasks_summary.append({
            "id": t.get("id"),
            "category": t.get("category"),
            "subject": t.get("subject"),
            "title": t.get("title"),
            "deadline": t.get("deadline")
        })

    completed = [a for a in assignments if a.get("status") == "submitted"]
    completed.sort(key=lambda x: x.get("completedAt") or x.get("deadline") or "", reverse=True)
    recent_completed_summary = [
        {"subject": c.get("subject"), "title": c.get("title"), "completedAt": c.get("completedAt")}
        for c in completed[:6]
    ]

    prompt = f"""現在は {now_str} です。
ユーザーは東京理科大学の学生で、学業（LETUS）や個人のタスクを管理しています。
ユーザーからの発言を自然言語解析し、適切なアクションとスロット（情報）の充足度を判定してください。

【直近の会話履歴（過去10分以内・古い順）】:
{history_text}

【既存の入力中ドラフト（前回の会話コンテキスト）】:
{json.dumps(existing_draft, ensure_ascii=False, indent=2) if existing_draft else "なし（新規の発言）"}

【デフォルト締切（今週末・締切未指定時の仮設定用）】:
{default_deadline_iso} ({default_deadline_display})

【現在の未完了タスク一覧】:
{json.dumps(tasks_summary, ensure_ascii=False, indent=2)}

【直近で完了したタスク（最新6件・新しい順）】:
{json.dumps(recent_completed_summary, ensure_ascii=False, indent=2)}

【タスク追加（add）の対話ルール】:
- タスクに必要な情報:
  1. `title`: タスクの内容（例: レポート提出、小テスト、シグマ 14-24mm 曇り確認、原稿暗記など）
  2. `subject`: 科目名または分類（例: 電磁気学、英語、個人、機材、アルバイトなど）。指定がなければ「一般」または「個人」。
  3. `deadline`: 締切日時（YYYY-MM-DDTHH:MM:SS 形式）。
  4. `category`: "academic"（大学・授業課題）または "personal"（個人・日常・機材・ToDo）
  5. `priority`: "high" | "medium" | "low"
  6. `memo`: ユーザーの補足や希望
  7. `is_default_deadline`: 締切がユーザーによって明示されず、デフォルト締切（今週末）を仮設定した場合は true、ユーザーが明示的に日時を指定した場合は false

- 会話文脈の継承ルール（最重要）:
  - ユーザーが「タスクとして登録」「お願い」「はい」「追加して」「よろしく」「登録して」などの短い返答や承諾をした場合、直前の【直近の会話履歴】や【既存の入力中ドラフト】から、直前に話題になっていたタスク内容（レンズ確認、片付け、レポート等）を特定し、必ず action: "add" を生成すること。決して「新しいタスクですね！どのようなタスクを追加しますか？」などと白紙で聞き直してはならない。
  - ユーザーが締切のみを返信した場合（例: 「来週火曜の18時で」「明日の夜まで」など）は、既存のドラフトの title や subject を引き継ぎ、deadline のみを更新して action: "add", is_ready: true を出力すること。

- 締切の決定ルール (`is_ready`):
  - 個人タスク（category == "personal": 機材チェック、日常ToDo、買い物、個人メモなど）:
    - ユーザーから明示的な日時指定がない場合: 上記の【デフォルト締切】（{default_deadline_iso}）を `deadline` に自動設定し、`is_default_deadline` を true、`is_ready` を true にしてください。（ユーザーに何度も日時を尋問せず、ワンタップで確定できる確認カードを即座に提示するため）
    - ユーザーから明示的な日時指定がある場合（例: 「明日」「土曜正午」「10/25」など）: 現在日時 {now_str} を基準に正確に計算して `deadline` にセットし、`is_default_deadline` を false、`is_ready` を true にしてください。
  - 学業タスク（category == "academic": 大学の課題、レポート提出、小テストなど）:
    - 締切に関する言及が一切ない場合: `is_ready` を false にし、`ask_question` で締切を優しく質問してください。ただし、ユーザーが「未定」「適当でいい」「特にない」「決まってない」と答えた場合は、デフォルト締切をセットして `is_default_deadline`: true, `is_ready`: true にしてください。

- 雑談・相談（chat）時のドラフト仮生成:
  - ユーザーが備品点検や作業メモ（例: 「シグマ 14 24 くもり 確認」「本を買う」など）をつぶやき、action: "chat" と判定した場合でも、将来のタスク登録に備えて `draft` フィールドに想定される title（「シグマ 14-24mm 曇り確認」等）, subject（「機材/個人」等）, category（"personal"）を生成して含めてください。

【その他のアクション】:
- "complete": タスク完了（未完了タスクから一致するIDを特定。複数言及時は matched_task_ids に全て含める）
- "list": 残りタスク確認（「今のタスク」「課題教えて」など）
- "completed_list": 完了したタスクの確認（「完了した課題教えて」「最近終わったタスク」など）
- "sync": LETUS同期
- "cancel": キャンセル
- "chat": 質問や雑談

【出力フォーマット（必ずこのJSONのみを出力してください）】:
```json
{{
  "action": "add" | "complete" | "list" | "completed_list" | "sync" | "chat" | "cancel",
  "is_ready": true | false,
  "draft": {{
    "title": "タスク名",
    "subject": "科目または分類",
    "deadline": "YYYY-MM-DDTHH:MM:SS",
    "is_default_deadline": true | false,
    "category": "academic" | "personal",
    "priority": "high" | "medium" | "low",
    "memo": "詳細メモ"
  }},
  "ask_question": "不足情報がある場合の質問文",
  "matched_task_ids": ["completeの場合のIDリスト（複数可）"],
  "matched_task_id": "completeの場合の代表ID（単一の場合）",
  "matched_task_title": "completeの場合のタイトル",
  "reply_text": "自然な返信文"
}}
```

ユーザーの最新メッセージ: 「{user_msg}」
"""
    ai_raw, model_used = call_gemini(prompt, api_key)
    parsed = parse_json_from_ai(ai_raw)
    parsed["_model_used"] = model_used
    return parsed

# -------------------------------------------------------------
# Discord Confirmation View (Buttons)
# -------------------------------------------------------------
class TaskConfirmView(discord.ui.View):
    def __init__(self, user_id: int, draft: dict, model_used: str, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.draft = draft
        self.model_used = model_used
        self.message = None

    @discord.ui.button(label="この内容で追加する", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ この操作はリクエストした本人のみ実行できます。", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        loop = asyncio.get_running_loop()
        title = self.draft.get("title") or "無題タスク"
        subject = self.draft.get("subject") or "一般"
        category = self.draft.get("category", "personal")
        priority = self.draft.get("priority", "medium")
        dl = self.draft.get("deadline")
        memo = self.draft.get("memo") or "Discordより登録"
        
        if not dl:
            dl = (datetime.now(JST) + timedelta(days=1)).strftime("%Y-%m-%dT23:59:00")

        new_id = f"custom-{datetime.now(JST).strftime('%Y%m%d%H%M%S')}"
        new_item = {
            "id": new_id,
            "category": category,
            "subject": subject,
            "title": title,
            "deadline": dl,
            "submissionMethod": "Discord Bot",
            "status": "not_started",
            "priority": priority,
            "memo": memo,
            "folderPath": None,
            "createdAt": datetime.now(JST).isoformat(),
            "completedAt": None
        }

        with DATA_LOCK:
            assignments = load_assignments()
            assignments.append(new_item)
            save_assignments(assignments)
        clear_user_session(self.user_id)
        clear_chat_history(self.user_id)

        schedule_git_push(f"Add task via Discord: {title}", delay=60.0)

        try:
            dl_dt = datetime.strptime(dl, "%Y-%m-%dT%H:%M:%S")
            dl_display = dl_dt.strftime("%Y年%m月%d日(%a) %H:%M")
        except Exception:
            dl_display = dl

        embed = discord.Embed(
            title="✅ タスクを正式に追加しました！",
            description=f"**[{subject}]** {title}",
            color=0x2ecc71
        )
        embed.add_field(name="⏰ 締切", value=dl_display, inline=True)
        embed.add_field(name="🏷️ 分類", value="📚 学業" if category == "academic" else "👤 自分", inline=True)
        embed.add_field(name="🔥 優先度", value=priority.upper(), inline=True)
        if memo and memo != "Discordより登録":
            embed.add_field(name="📝 メモ", value=memo, inline=False)
        embed.add_field(name="🌐 Webダッシュボード", value="[Task Radarを開く](https://fujitomo1123581321-byte.github.io/task-radar/)", inline=False)
        embed.set_footer(text=f"AI Brain: {self.model_used} | Task ID: {new_id} | Web同期完了")

        await interaction.followup.send(embed=embed)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ この操作はリクエストした本人のみ実行できます。", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        clear_user_session(self.user_id)
        clear_chat_history(self.user_id)
        await interaction.response.edit_message(content="❌ タスクの追加をキャンセルしました。", embed=None, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        clear_user_session(self.user_id)
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

# -------------------------------------------------------------
# Discord Bot Client
# -------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

_periodic_sync_started = False

async def cloud_periodic_letus_sync():
    """24/7 Cloud Background Loop: checks LETUS every 1 hour, auto-pushes to GitHub/Web and notifies Discord"""
    await asyncio.sleep(45)  # Wait 45s for bot setup & port binding
    while True:
        try:
            cfg = load_config()
            cal_url = cfg.get("letus_calendar_url") or os.environ.get("LETUS_CALENDAR_URL")
            if cal_url:
                print(f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] [Cloud LETUS Sync] Starting hourly background sync...")
                import sync_letus
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, sync_letus.fetch_and_sync)
                new_items = (result or {}).get("new_items", [])
                updated_count = (result or {}).get("updated_count", 0)

                if new_items or updated_count > 0:
                    commit_msg = f"Auto-sync LETUS assignments (New: {len(new_items)}, Updated: {updated_count})"
                    await loop.run_in_executor(None, git_push_sync, commit_msg)
                    print(f"[Cloud LETUS Sync] Pushed to GitHub. New: {len(new_items)}, Updated: {updated_count}")

                    # Notify Discord channel if new tasks found!
                    upcoming_new = [x for x in new_items if x.get("status") != "submitted"]
                    if upcoming_new:
                        for guild in bot.guilds:
                            for channel in guild.text_channels:
                                if any(k in channel.name.lower() for k in ["課題", "タスク", "task", "todo", "bot"]):
                                    try:
                                        embed = discord.Embed(
                                            title="📥 LETUS新着課題を検出しました（クラウド自動同期）",
                                            description="\n".join([f"・**[{x.get('subject')}] {x.get('title')}**\n  締切: `{x.get('deadline')}`" for x in upcoming_new]),
                                            color=0x2ecc71
                                        )
                                        embed.add_field(name="🌐 Webダッシュボード", value="[Task Radarを開く](https://fujitomo1123581321-byte.github.io/task-radar/)", inline=False)
                                        await channel.send(embed=embed)
                                        break
                                    except Exception as notify_err:
                                        print(f"[Cloud Notify Error] {notify_err}")
                else:
                    print("[Cloud LETUS Sync] Check complete. No changes.")
            else:
                print("[Cloud LETUS Sync] Skipped: LETUS_CALENDAR_URL not configured.")
        except Exception as e:
            print(f"[Cloud LETUS Sync Error] {e}")
        await asyncio.sleep(3600)  # Repeat every 1 hour

@bot.event
async def on_ready():
    global _periodic_sync_started
    if not _periodic_sync_started:
        _periodic_sync_started = True
        bot.loop.create_task(cloud_periodic_letus_sync())
    print(f"==================================================")
    print(f"🤖 Bot is ONLINE: {bot.user.name} (ID: {bot.user.id})")
    print(f"🏠 Connected Guilds: {len(bot.guilds)}")
    for g in bot.guilds:
        print(f"   - {g.name} (ID: {g.id})")
    print(f"🧠 AI Engine: Google Gemini (Interactive Task Management)")
    print(f"==================================================")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="課題と締切 | 話しかけてタスク追加"
        )
    )

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    is_mentioned = bot.user in message.mentions
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_command = message.content.startswith("!")
    is_task_channel = False
    if hasattr(message.channel, "name"):
        ch_name = str(message.channel.name).lower()
        if any(k in ch_name for k in ["課題", "タスク", "task", "todo", "bot"]):
            is_task_channel = True

    is_reply_to_bot = False
    if message.reference and message.reference.resolved:
        ref_msg = message.reference.resolved
        if getattr(ref_msg, "author", None) and ref_msg.author.id == bot.user.id:
            is_reply_to_bot = True

    if is_command:
        await bot.process_commands(message)
        return

    user_id = message.author.id
    has_active_session = (get_user_session(user_id) is not None)

    if is_mentioned or is_dm or is_task_channel or has_active_session or is_reply_to_bot:
        clean_content = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not clean_content:
            await message.reply("👋 何か用事かな？「〜のレポート追加して」「〜終わった」「残りの課題教えて」など何でも話しかけてね！")
            return

        if clean_content.lower() in ["キャンセル", "cancel", "やめる", "とりけし", "取り消し"]:
            if has_active_session:
                clear_user_session(user_id)
                clear_chat_history(user_id)
                await message.reply("❌ タスクの追加をキャンセルしました。")
                return

        async with message.channel.typing():
            try:
                loop = asyncio.get_running_loop()
                api_key = CONFIG.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")

                # 直前までの会話履歴を取得し、今回のユーザー発言を履歴に追加
                prev_history = get_chat_history(user_id)
                add_chat_history(user_id, "user", clean_content)

                res = await loop.run_in_executor(None, analyze_dialog_with_gemini, user_id, clean_content, api_key, prev_history)
                action = res.get("action", "chat")
                model_used = res.get("_model_used", "Gemini")

                # Handle ACTION: ADD
                if action == "add":
                    is_ready = res.get("is_ready", False)
                    draft = res.get("draft") or {}

                    if not is_ready:
                        user_sessions[user_id] = {"draft": draft, "updated_at": time.time()}
                        question = res.get("ask_question") or "締切は何月何日の何時頃でしょうか？（または『特にない』とお答えください）"
                        embed = discord.Embed(title="📝 情報を教えてください", description=question, color=0x3498db)
                        embed.set_footer(text="💡 キャンセルしたい場合は「キャンセル」と返信してください")
                        await message.reply(embed=embed)
                        add_chat_history(user_id, "assistant", question)
                        return

                    user_sessions[user_id] = {"draft": draft, "updated_at": time.time()}
                    title = draft.get("title") or "無題タスク"
                    subject = draft.get("subject") or "一般"
                    category = draft.get("category", "personal")
                    priority = draft.get("priority", "medium")
                    dl = draft.get("deadline")
                    memo = draft.get("memo")
                    is_default_dl = draft.get("is_default_deadline", False)

                    try:
                        dl_dt = datetime.strptime(dl, "%Y-%m-%dT%H:%M:%S")
                        dl_display = dl_dt.strftime("%Y年%m月%d日(%a) %H:%M")
                        if is_default_dl:
                            dl_display += "（仮設定・今週末）"
                    except Exception:
                        dl_display = dl or "未定"

                    embed = discord.Embed(
                        title="📋 【登録確認】以下の内容で追加しますか？",
                        description=f"**[{subject}]** {title}",
                        color=0x3498db
                    )
                    embed.add_field(name="⏰ 締切日時", value=dl_display, inline=True)
                    embed.add_field(name="🏷️ カテゴリー", value="📚 学業" if category == "academic" else "👤 自分", inline=True)
                    embed.add_field(name="🔥 優先度", value=priority.upper(), inline=True)
                    if memo and memo != "Discordより登録":
                        embed.add_field(name="📝 メモ", value=memo, inline=False)

                    if is_default_dl:
                        embed.set_footer(text=f"AI Brain: {model_used} | 💡 締切を変更したい場合は「明日の18時で」「来週月曜まで」などと返信してください")
                    else:
                        embed.set_footer(text=f"AI Brain: {model_used} | 下のボタンを押して確定してください")

                    view = TaskConfirmView(user_id=user_id, draft=draft, model_used=model_used)
                    sent_msg = await message.reply(embed=embed, view=view)
                    view.message = sent_msg
                    add_chat_history(user_id, "assistant", f"タスク『{title}』の登録確認カードを提示（締切: {dl_display}）")

                # Handle ACTION: COMPLETE
                elif action == "complete":
                    clear_user_session(user_id)
                    matched_ids = res.get("matched_task_ids") or []
                    if not matched_ids and res.get("matched_task_id"):
                        matched_ids = [res.get("matched_task_id")]

                    completed_targets = []
                    with DATA_LOCK:
                        assignments = load_assignments()
                        if matched_ids:
                            for mid in matched_ids:
                                for a in assignments:
                                    if str(a.get("id")) == str(mid):
                                        if a.get("status") != "submitted":
                                            a["status"] = "submitted"
                                            a["completedAt"] = datetime.now(JST).isoformat()
                                            completed_targets.append(a)
                                        break

                        # Fallback if no target matched via ID: query by title
                        if not completed_targets:
                            query = res.get("matched_task_title") or clean_content
                            for a in assignments:
                                if a.get("status") != "submitted" and (a.get("title") in query or query in a.get("title")):
                                    a["status"] = "submitted"
                                    a["completedAt"] = datetime.now(JST).isoformat()
                                    completed_targets.append(a)
                                    break

                        if completed_targets:
                            save_assignments(assignments)
                            titles = [t.get("title") for t in completed_targets]
                            schedule_git_push(f"Complete task(s) via Discord: {', '.join(titles)}", delay=60.0)

                    if completed_targets:
                        if len(completed_targets) == 1:
                            target = completed_targets[0]
                            embed = discord.Embed(
                                title="🎉 タスクを完了にしました！",
                                description=f"**[{target.get('subject')}]** {target.get('title')}\nお疲れ様でした！",
                                color=0xf1c40f
                            )
                        else:
                            items_str = "\n".join([f"• **[{t.get('subject')}]** {t.get('title')}" for t in completed_targets])
                            embed = discord.Embed(
                                title=f"🎉 {len(completed_targets)} 件のタスクを一括完了にしました！",
                                description=f"{items_str}\n\nお疲れ様でした！素晴らしいペースです！👏",
                                color=0xf1c40f
                            )
                        embed.set_footer(text=f"AI Brain: {model_used} | Webダッシュボードに即時反映済み")
                        await message.reply(embed=embed)
                        add_chat_history(user_id, "assistant", f"タスク完了: {', '.join(titles)}")
                    else:
                        await message.reply("🤔 完了にしたいタスクが見つかりませんでした。\n「未完了の課題を教えて」と聞いて名前を確認してみてください！")

                # Handle ACTION: COMPLETED_LIST
                elif action == "completed_list":
                    with DATA_LOCK:
                        assignments = load_assignments()
                    completed = [a for a in assignments if a.get("status") == "submitted"]
                    completed.sort(key=lambda x: x.get("completedAt") or x.get("deadline") or "", reverse=True)

                    embed = discord.Embed(title=f"🎉 完了済みの課題・タスク（全 {len(completed)} 件）", color=0x2ecc71)
                    if not completed:
                        embed.description = "まだ完了済みのタスクはありません。頑張りましょう！💪"
                    else:
                        lines = []
                        for idx, item in enumerate(completed[:8], 1):
                            subject = item.get("subject", "一般")
                            title = item.get("title", "無題")
                            comp_at = item.get("completedAt")
                            if comp_at:
                                try:
                                    dt = datetime.fromisoformat(comp_at)
                                    comp_txt = dt.strftime("%m/%d %H:%M 完了")
                                except Exception:
                                    comp_txt = "完了済み"
                            else:
                                comp_txt = "完了済み"
                            lines.append(f"**{idx}. [{subject}] {title}**\n   ✅ {comp_txt}")

                        embed.description = "\n\n".join(lines)
                        if len(completed) > 8:
                            embed.set_footer(text=f"他 {len(completed) - 8} 件の完了タスクがあります | Webダッシュボードで全件確認可能")

                    embed.add_field(name="🌐 ダッシュボード", value="[Task Radarを開く](https://fujitomo1123581321-byte.github.io/task-radar/)", inline=False)
                    await message.reply(embed=embed)
                    add_chat_history(user_id, "assistant", f"完了タスク一覧を提示（{len(completed)}件）")

                # Handle ACTION: LIST
                elif action == "list":
                    assignments = load_assignments()
                    uncompleted = [a for a in assignments if a.get("status") != "submitted"]
                    now_dt = datetime.now(JST).replace(tzinfo=None)

                    def parse_dl(item):
                        try:
                            return datetime.strptime(item.get("deadline", "9999-12-31T23:59:00"), "%Y-%m-%dT%H:%M:%S")
                        except Exception:
                            return datetime.max
                    uncompleted.sort(key=parse_dl)

                    embed = discord.Embed(title=f"📋 未完了の課題・タスク一覧（残り {len(uncompleted)} 件）", color=0x3498db)
                    if not uncompleted:
                        embed.description = "🎉 現在未完了のタスクはありません！素晴らしいです！"
                    else:
                        lines = []
                        for idx, item in enumerate(uncompleted[:8], 1):
                            dl_str = item.get("deadline")
                            remaining_txt = ""
                            try:
                                dl_dt = datetime.strptime(dl_str, "%Y-%m-%dT%H:%M:%S")
                                diff = dl_dt - now_dt
                                if diff.total_seconds() < 0:
                                    remaining_txt = "⚠️ 期限切れ"
                                elif diff.days == 0:
                                    hours = int(diff.seconds // 3600)
                                    mins = int((diff.seconds % 3600) // 60)
                                    remaining_txt = f"🚨 残り **{hours}時間{mins}分**"
                                else:
                                    remaining_txt = f"⏳ 残り **{diff.days}日**"
                                dl_formatted = dl_dt.strftime("%m/%d %H:%M")
                            except Exception:
                                dl_formatted = dl_str

                            subject = item.get("subject", "一般")
                            title = item.get("title", "無題")
                            memo_hint = f" ({item.get('memo')})" if item.get("memo") and item.get("memo") != "LETUSより自動同期" else ""
                            lines.append(f"**{idx}. [{subject}] {title}{memo_hint}**\n   締切: {dl_formatted} ({remaining_txt})")

                        embed.description = "\n\n".join(lines)
                        if len(uncompleted) > 8:
                            embed.set_footer(text=f"他 {len(uncompleted) - 8} 件のタスクがあります | Webダッシュボードで確認可能")

                    embed.add_field(name="🌐 ダッシュボード", value="[Task Radarを開く](https://fujitomo1123581321-byte.github.io/task-radar/)", inline=False)
                    await message.reply(embed=embed)
                    add_chat_history(user_id, "assistant", f"未完了タスク一覧を提示（{len(uncompleted)}件）")

                # Handle ACTION: SYNC
                elif action == "sync":
                    await message.reply("🔄 LETUSとカレンダーを同期しています...")
                    try:
                        import sync_letus
                        await loop.run_in_executor(None, sync_letus.fetch_and_sync)
                        await loop.run_in_executor(None, git_push_sync, "Auto-sync LETUS assignments via Discord")
                        await message.reply("✅ LETUSの最新課題を同期し、Webダッシュボードへ反映しました！")
                        add_chat_history(user_id, "assistant", "LETUS同期完了")
                    except Exception as e:
                        await message.reply(f"⚠️ 同期中にエラーが発生しました: {e}")

                # Handle ACTION: CANCEL
                elif action == "cancel":
                    clear_user_session(user_id)
                    clear_chat_history(user_id)
                    await message.reply("❌ タスクの追加をキャンセルしました。")

                # Handle ACTION: CHAT
                else:
                    tentative_draft = res.get("draft")
                    if tentative_draft and tentative_draft.get("title"):
                        user_sessions[user_id] = {"draft": tentative_draft, "updated_at": time.time()}

                    reply_text = res.get("reply_text") or "承知いたしました！"
                    embed = discord.Embed(description=reply_text, color=0x9b59b6)
                    embed.set_footer(text=f"AI Brain: {model_used}")
                    await message.reply(embed=embed)
                    add_chat_history(user_id, "assistant", reply_text)

            except Exception as e:
                print(f"[Error in message handler]: {e}")
                await message.reply(f"⚠️ 処理中にエラーが発生しました: {e}")

# -------------------------------------------------------------
# Slash / Text Commands
# -------------------------------------------------------------
@bot.command(name="help")
async def cmd_help(ctx):
    embed = discord.Embed(
        title="🤖 Task Radar AI Bot の使い方",
        description="自然言語で話しかけるだけで、課題の追加・完了・確認ができます！\n24時間クラウド対応。",
        color=0x3498db
    )
    embed.add_field(
        name="📝 タスクを追加する",
        value="・「明日の18時までに電磁気レポート提出」\n・「締めは土曜正午だけどできれば金曜中に原稿覚える」\n・ボタンでワンタップ確定！",
        inline=False
    )
    embed.add_field(name="✅ タスクを完了にする", value="・「ミニテスト4終わった」\n・「原稿完了」", inline=False)
    embed.add_field(name="📋 課題を確認する", value="・「直近の締切教えて」\n・「!list」\n・「完了した課題教えて」または「!done」", inline=False)
    embed.add_field(name="🔄 LETUSを同期する", value="・「LETUS同期して」または「!sync」", inline=False)
    embed.add_field(name="🌐 Webダッシュボード", value="[Task Radarを開く](https://fujitomo1123581321-byte.github.io/task-radar/)", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="completed", aliases=["done"])
async def cmd_completed(ctx):
    with DATA_LOCK:
        assignments = load_assignments()
    completed = [a for a in assignments if a.get("status") == "submitted"]
    completed.sort(key=lambda x: x.get("completedAt") or x.get("deadline") or "", reverse=True)

    embed = discord.Embed(title=f"🎉 直近で完了したタスク一覧（最新 {min(8, len(completed))} 件）", color=0x2ecc71)
    if not completed:
        embed.description = "完了したタスクはまだありません。"
    else:
        lines = []
        for idx, item in enumerate(completed[:8], 1):
            subject = item.get("subject", "一般")
            title = item.get("title", "無題")
            comp_at = item.get("completedAt")
            if comp_at:
                try:
                    dt = datetime.fromisoformat(comp_at)
                    comp_txt = dt.strftime("%m/%d %H:%M 完了")
                except Exception:
                    comp_txt = "完了済み"
            else:
                comp_txt = "完了済み"
            lines.append(f"**{idx}. [{subject}] {title}**\n   ✅ {comp_txt}")
        embed.description = "\n\n".join(lines)
        if len(completed) > 8:
            embed.set_footer(text=f"他 {len(completed) - 8} 件の完了タスクがあります")
    embed.add_field(name="🌐 Webダッシュボード", value="[Task Radar](https://fujitomo1123581321-byte.github.io/task-radar/)", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="list")
async def cmd_list(ctx):
    assignments = load_assignments()
    uncompleted = [a for a in assignments if a.get("status") != "submitted"]
    now_dt = datetime.now(JST).replace(tzinfo=None)

    def parse_dl(item):
        try:
            return datetime.strptime(item.get("deadline", "9999-12-31T23:59:00"), "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return datetime.max
    uncompleted.sort(key=parse_dl)

    embed = discord.Embed(title=f"📋 未完了の課題・タスク一覧（{len(uncompleted)} 件）", color=0x3498db)
    if not uncompleted:
        embed.description = "🎉 未完了のタスクはありません！"
    else:
        lines = []
        for idx, item in enumerate(uncompleted[:8], 1):
            dl_str = item.get("deadline")
            try:
                dl_dt = datetime.strptime(dl_str, "%Y-%m-%dT%H:%M:%S")
                diff = dl_dt - now_dt
                if diff.total_seconds() < 0:
                    remaining_txt = "⚠️ 期限切れ"
                elif diff.days == 0:
                    hours = int(diff.seconds // 3600)
                    mins = int((diff.seconds % 3600) // 60)
                    remaining_txt = f"🚨 残り **{hours}時間{mins}分**"
                else:
                    remaining_txt = f"⏳ 残り **{diff.days}日**"
                dl_formatted = dl_dt.strftime("%m/%d %H:%M")
            except Exception:
                dl_formatted = dl_str
                remaining_txt = ""
            lines.append(f"**{idx}. [{item.get('subject', '一般')}] {item.get('title', '無題')}**\n   締切: {dl_formatted} ({remaining_txt})")
        embed.description = "\n\n".join(lines)
    embed.add_field(name="🌐 Webダッシュボード", value="[Task Radar](https://fujitomo1123581321-byte.github.io/task-radar/)", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="sync")
async def cmd_sync(ctx):
    await ctx.send("🔄 LETUS同期を開始します...")
    try:
        import sync_letus
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, sync_letus.fetch_and_sync)
        await loop.run_in_executor(None, git_push_sync, "Sync LETUS assignments via !sync")
        await ctx.send("✅ LETUSの課題を同期し、Webダッシュボードへ反映しました！")
    except Exception as e:
        await ctx.send(f"⚠️ 同期中にエラーが発生しました: {e}")

# -------------------------------------------------------------
# Background Health Server for Render (Standard Threaded Pattern)
# -------------------------------------------------------------
import http.server
import threading

class HealthHandler(http.server.BaseHTTPRequestHandler):
    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        normalized_path = self.path.split("?")[0].rstrip("/")
        if normalized_path in ("/api/sync", "/api/sync/"):
            try:
                import sync_letus
                res = sync_letus.fetch_and_sync() or {}
                new_items = res.get("new_items", [])
                updated_count = res.get("updated_count", 0)
                if new_items or updated_count > 0:
                    git_push_sync(f"Sync LETUS assignments via /api/sync (New: {len(new_items)}, Updated: {updated_count})")
                resp = json.dumps({"status": "ok", "new_count": len(new_items), "updated_count": updated_count}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp)
                return
            except Exception as e:
                resp = json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp)
                return

        if normalized_path in ("/api/assignments", "/assignments.json"):
            with DATA_LOCK:
                assignments = load_assignments()
                resp = json.dumps(assignments, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self._send_cors()
            self.end_headers()
            self.wfile.write(resp)
            return

        with DATA_LOCK:
            assignments = load_assignments()
            count = len(assignments)

        data = {
            "status": "online",
            "app": "Task Radar Discord AI Bot",
            "bot_user": bot.user.name if bot.user else "Starting...",
            "tasks_count": count,
            "timestamp": datetime.now(JST).isoformat()
        }
        resp = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self._send_cors()
        self.end_headers()
        self.wfile.write(resp)

    def do_POST(self):
        if self.path in ("/api/set-calendar-url", "/api/set-calendar-url/"):
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                payload = json.loads(body.decode("utf-8"))
                secret = payload.get("secret", "")
                expected_secret = "TASK_RADAR_SECURE_SYNC_2026"
                if secret != expected_secret and secret != CONFIG.get("discord_bot_token"):
                    self.send_response(403)
                    self._send_cors()
                    self.end_headers()
                    self.wfile.write(b'{"error": "Forbidden"}')
                    return

                new_url = payload.get("url", "").strip()
                if new_url and new_url.startswith("http"):
                    CONFIG["letus_calendar_url"] = new_url
                    url_file = os.path.join(BASE_DIR, "letus_url.txt")
                    with open(url_file, "w", encoding="utf-8") as f:
                        f.write(new_url)

                    # Trigger instant sync in background
                    import sync_letus
                    sync_res = sync_letus.fetch_and_sync() or {}
                    new_items = sync_res.get("new_items", [])
                    updated_count = sync_res.get("updated_count", 0)
                    if new_items or updated_count > 0:
                        git_push_sync(f"Auto-sync LETUS assignments via Cloud Endpoint (New: {len(new_items)}, Updated: {updated_count})")

                    resp = json.dumps({"status": "ok", "message": "Calendar URL saved and sync executed", "new_items": len(new_items)}, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self._send_cors()
                    self.end_headers()
                    self.wfile.write(resp)
                    return
                else:
                    self.send_response(400)
                    self._send_cors()
                    self.end_headers()
                    self.wfile.write(b'{"error": "Invalid URL"}')
                    return
            except Exception as e:
                resp = json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors()
                self.end_headers()
                self.wfile.write(resp)
                return

        if self.path in ("/api/task-status", "/api/task-status/"):
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                payload = json.loads(body.decode("utf-8"))

                # Support single task or batch items
                items_to_update = payload.get("items")
                if not items_to_update:
                    if "id" in payload:
                        items_to_update = [payload]
                    else:
                        items_to_update = []

                if not items_to_update:
                    self.send_response(400)
                    self._send_cors()
                    self.end_headers()
                    self.wfile.write(b'{"error": "Missing id/status or items list"}')
                    return

                with DATA_LOCK:
                    assignments = load_assignments()
                    updated_targets = []

                    for item in items_to_update:
                        task_id = item.get("id")
                        new_status = item.get("status")
                        task_title = item.get("title")

                        target = None
                        if task_id:
                            for a in assignments:
                                if str(a.get("id")) == str(task_id):
                                    target = a
                                    break
                        if not target and task_title:
                            for a in assignments:
                                if a.get("title", "").strip() == str(task_title).strip():
                                    target = a
                                    break

                        if target and new_status:
                            target["status"] = new_status
                            if new_status == "submitted":
                                target["completedAt"] = datetime.now(JST).isoformat()
                            else:
                                target["completedAt"] = None
                            updated_targets.append(target)

                    if updated_targets:
                        save_assignments(assignments)
                        summary_title = updated_targets[0].get('title', 'task')
                        schedule_git_push(f"Update {len(updated_targets)} task(s) status via Web: {summary_title}", delay=60.0)

                        resp = json.dumps({
                            "status": "ok",
                            "updated_count": len(updated_targets),
                            "items": [{"id": t.get("id"), "status": t.get("status"), "title": t.get("title")} for t in updated_targets]
                        }, ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(resp)))
                        self._send_cors()
                        self.end_headers()
                        self.wfile.write(resp)
                        print(f"🌐 [Web Sync API] Successfully updated {len(updated_targets)} task(s) via Web")
                    else:
                        self.send_response(404)
                        self._send_cors()
                        self.end_headers()
                        self.wfile.write(b'{"error": "Task not found"}')
            except Exception as e:
                self.send_response(500)
                self._send_cors()
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        elif self.path in ("/api/task-delete", "/api/task-delete/"):
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                payload = json.loads(body.decode("utf-8"))
                task_id = payload.get("id")
                title = payload.get("title", task_id)

                with DATA_LOCK:
                    assignments = load_assignments()
                    new_assignments = [a for a in assignments if str(a.get("id")) != str(task_id)]
                    save_assignments(new_assignments)
                    schedule_git_push(f"Delete task '{title}' via Web", delay=60.0)

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors()
                self.end_headers()
                self.wfile.write(b'{"status": "ok", "action": "deleted"}')
                print(f"🌐 [Web Sync API] Task '{task_id}' deleted via Web")
            except Exception as e:
                self.send_response(500)
                self._send_cors()
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

        elif self.path in ("/api/task-ignore", "/api/task-ignore/"):
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                payload = json.loads(body.decode("utf-8"))
                title = payload.get("title")
                task_id = payload.get("id")

                with DATA_LOCK:
                    assignments = load_assignments()
                    new_assignments = [a for a in assignments if a.get("title") != title and str(a.get("id")) != str(task_id)]
                    save_assignments(new_assignments)

                    # Persist in ignored_titles.json (committed to git so GitHub Actions never resurrects it)
                    if title:
                        cur_ignored = load_ignored_titles()
                        if title not in cur_ignored:
                            cur_ignored.append(title)
                            save_ignored_titles(cur_ignored)

                    # Add to local config.json ignored_titles if present
                    cfg = load_config()
                    if "ignored_titles" not in cfg:
                        cfg["ignored_titles"] = []
                    if title and title not in cfg["ignored_titles"]:
                        cfg["ignored_titles"].append(title)
                        if os.path.exists(CONFIG_PATH):
                            try:
                                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                            except Exception:
                                pass

                    schedule_git_push(f"Ignore task '{title}' via Web", delay=60.0)

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors()
                self.end_headers()
                self.wfile.write(b'{"status": "ok", "action": "ignored"}')
                print(f"🌐 [Web Sync API] Task '{title}' ignored via Web")
            except Exception as e:
                self.send_response(500)
                self._send_cors()
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

        elif self.path in ("/api/task-add", "/api/task-add/"):
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                payload = json.loads(body.decode("utf-8"))
                task = payload.get("task") or payload
                
                title = task.get("title", "").strip()
                if not title:
                    self.send_response(400)
                    self._send_cors()
                    self.end_headers()
                    self.wfile.write(b'{"error": "Missing task title"}')
                    return

                if not task.get("id"):
                    task["id"] = "custom-" + datetime.now(JST).strftime("%Y%m%d%H%M%S")
                if not task.get("createdAt"):
                    task["createdAt"] = datetime.now(JST).isoformat()
                if not task.get("status"):
                    task["status"] = "not_started"

                with DATA_LOCK:
                    assignments = load_assignments()
                    # Check for existing id
                    assignments = [a for a in assignments if str(a.get("id")) != str(task["id"])]
                    assignments.append(task)
                    save_assignments(assignments)
                    schedule_git_push(f"Add task via Web: {title}", delay=60.0)

                resp = json.dumps({"status": "ok", "action": "added", "task": task}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(resp)))
                self._send_cors()
                self.end_headers()
                self.wfile.write(resp)
                print(f"🌐 [Web Sync API] New task '{title}' added via Web")
            except Exception as e:
                self.send_response(500)
                self._send_cors()
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

        elif self.path in ("/api/task-update", "/api/task-update/"):
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                payload = json.loads(body.decode("utf-8"))
                task = payload.get("task") or payload
                task_id = str(task.get("id", ""))

                if not task_id:
                    self.send_response(400)
                    self._send_cors()
                    self.end_headers()
                    self.wfile.write(b'{"error": "Missing task id"}')
                    return

                with DATA_LOCK:
                    assignments = load_assignments()
                    target = None
                    for a in assignments:
                        if str(a.get("id")) == task_id:
                            target = a
                            break

                    if target:
                        # Update fields
                        for k in ("title", "subject", "deadline", "category", "priority", "memo", "submissionMethod", "status"):
                            if k in task and task[k] is not None:
                                target[k] = task[k]
                        if target.get("status") == "submitted" and not target.get("completedAt"):
                            target["completedAt"] = datetime.now(JST).isoformat()
                        elif target.get("status") != "submitted":
                            target["completedAt"] = None

                        save_assignments(assignments)
                        schedule_git_push(f"Update task '{target.get('title')}' details via Web", delay=60.0)

                        resp = json.dumps({"status": "ok", "action": "updated", "task": target}, ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(resp)))
                        self._send_cors()
                        self.end_headers()
                        self.wfile.write(resp)
                        print(f"🌐 [Web Sync API] Task '{target.get('title')}' updated via Web")
                    else:
                        self.send_response(404)
                        self._send_cors()
                        self.end_headers()
                        self.wfile.write(b'{"error": "Task not found"}')
            except Exception as e:
                self.send_response(500)
                self._send_cors()
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

        else:
            self.send_response(404)
            self._send_cors()
            self.end_headers()
            self.wfile.write(b'{"error": "Endpoint not found"}')

    def log_message(self, format, *args):
        # Suppress noisy health ping access logs
        pass

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"🌐 [Cloud Server] Multi-threaded health check listener active on 0.0.0.0:{port}")

def start_self_ping():
    """Background keep-alive daemon: pings public health endpoint every 5 min to prevent 15-min free tier spin-down"""
    def _ping_loop():
        time.sleep(45)  # Wait for startup to complete
        app_url = os.environ.get("RENDER_EXTERNAL_URL") or "https://task-radar-us.onrender.com"
        ping_target = f"{app_url.rstrip('/')}/health"
        print(f"🔄 [Keep-Alive Engine] Active self-ping configured for {ping_target} (every 5 min)")

        while True:
            try:
                req = urllib.request.Request(
                    ping_target,
                    headers={"User-Agent": "TaskRadarInternalKeepAlive/1.0"}
                )
                with urllib.request.urlopen(req, timeout=20) as res:
                    if res.status == 200:
                        pass
            except Exception as e:
                print(f"[Keep-Alive Engine Notice] Ping attempt: {e}")
            time.sleep(300)  # 5 minutes

    t = threading.Thread(target=_ping_loop, daemon=True)
    t.start()

# -------------------------------------------------------------
# Main Runner
# -------------------------------------------------------------
def main():
    token = CONFIG.get("discord_bot_token") or os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("Error: 'discord_bot_token' is missing in config.json or environment variables.")
        sys.exit(1)

    # 1. Start background web server for Render port check
    start_health_server()

    # 2. Start internal keep-alive engine (pings public URL every 8 min to prevent sleep)
    start_self_ping()

    # 3. Run standard Discord Bot (discord.py manages event loop & automatic backoff)
    print("Starting Discord AI Bot with standard bot.run()...")
    bot.run(token)

if __name__ == "__main__":
    main()
