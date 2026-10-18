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

JST = timezone(timedelta(hours=9))

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

    return cfg

CONFIG = load_config()

def load_assignments():
    if not os.path.exists(JSON_PATH):
        try:
            url = "https://raw.githubusercontent.com/fujitomo1123581321-byte/task-radar/main/assignments.json"
            req = urllib.request.Request(url, headers={"User-Agent": "TaskRadarBot"})
            with urllib.request.urlopen(req, timeout=5) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception:
            return []
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_assignments(items):
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    with open(JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.INITIAL_ASSIGNMENTS = " + json.dumps(items, ensure_ascii=False, indent=2) + ";\n")

def sync_to_github_via_api(path: str, content_bytes: bytes, commit_msg: str, token: str, repo: str = "fujitomo1123581321-byte/task-radar"):
    """Update file in GitHub repo directly using GitHub REST API (No git CLI required in cloud)"""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "TaskRadarCloudBot",
        "Accept": "application/vnd.github.v3+json"
    }
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
        print(f"[GitHub API PUT Error] {path} HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:100]}")
        return False
    except Exception as e:
        print(f"[GitHub API PUT Error] {path} error: {e}")
        return False

def git_push_sync(commit_msg="Update assignments via Discord Bot"):
    """Dual-mode sync: GitHub API (Cloud) or Git Subprocess (Local)"""
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
            print(f"[Cloud Git Sync] Pushed to GitHub via API: {commit_msg}")
            return True
        except Exception as e:
            print(f"[Cloud Git Sync Warning] API sync failed, trying local git: {e}")

    try:
        subprocess.run(["git", "add", "assignments.json", "assignments.js"], cwd=BASE_DIR, check=True, capture_output=True)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=BASE_DIR, capture_output=True, text=True)
        if "assignments.json" in status.stdout or "assignments.js" in status.stdout:
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=BASE_DIR, check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR, check=True, capture_output=True)
            print(f"[Local Git Sync] Successfully pushed to GitHub: {commit_msg}")
            return True
        return False
    except Exception as e:
        print(f"[Git Sync Notice] {e}")
        return False

# -------------------------------------------------------------
# Session Management
# -------------------------------------------------------------
user_sessions = {}
SESSION_TIMEOUT = 600

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
def analyze_dialog_with_gemini(user_id: int, user_msg: str, api_key: str):
    assignments = load_assignments()
    now_dt = datetime.now(JST)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S (JST, %A)")

    session = get_user_session(user_id)
    existing_draft = session.get("draft") if session else None

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

    prompt = f"""現在は {now_str} です。
ユーザーは東京理科大学の学生で、学業（LETUS）や個人のタスクを管理しています。
ユーザーからの発言を自然言語解析し、適切なアクションとスロット（情報）の充足度を判定してください。

【既存の入力中ドラフト（前回の会話コンテキスト）】:
{json.dumps(existing_draft, ensure_ascii=False, indent=2) if existing_draft else "なし（新規の発言）"}

【現在の未完了タスク一覧】:
{json.dumps(tasks_summary, ensure_ascii=False, indent=2)}

【タスク追加（add）の対話ルール】:
- タスクに必要な情報:
  1. `title`: タスクの内容（例: レポート提出、小テスト、原稿を覚えるなど）
  2. `subject`: 科目名または分類（例: 電磁気学、英語、個人、アルバイトなど）。指定がなければ「一般」または「個人」。
  3. `deadline`: 締切日時（YYYY-MM-DDTHH:MM:SS 形式）。
  4. `memo`: ユーザーの補足や希望（例: 「できれば金曜中に完了したい」などの要望があれば、そのままメモ欄に記載してください）。
- 判定基準 (`is_ready`):
  - 締切に関する言及が一切ない場合（例: 「レポート追加」「タスク登録」のみ）:
    `is_ready` は false にし、`ask_question` で締切を優しく質問してください。
  - 締切が指定されている場合（例: 「土曜正午」「明日の18時」「10/25」など）:
    現在時刻 {now_str} を基準に正確に計算して `deadline` にセットし、`is_ready` は true にしてください。
    ※もし「締めは土曜正午だけどできれば金曜中に」と言われたら、締切は土曜正午（2026-10-17T12:00:00）にセットし、memoに「できれば金曜中に完了したい」と記載してください。

【その他のアクション】:
- "complete": タスク完了（未完了タスクから一致する `matched_task_id` を特定）
- "list": 残りタスク確認
- "sync": LETUS同期
- "cancel": キャンセル
- "chat": 質問や雑談

【出力フォーマット（必ずこのJSONのみを出力してください）】:
```json
{{
  "action": "add" | "complete" | "list" | "sync" | "chat" | "cancel",
  "is_ready": true | false,
  "draft": {{
    "title": "タスク名",
    "subject": "科目または分類",
    "deadline": "YYYY-MM-DDTHH:MM:SS",
    "category": "academic" | "personal",
    "priority": "high" | "medium" | "low",
    "memo": "詳細メモ（できれば金曜中などの補足があれば記載）"
  }},
  "ask_question": "不足情報がある場合の質問文",
  "matched_task_id": "completeの場合のID",
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

        assignments = load_assignments()
        assignments.append(new_item)
        save_assignments(assignments)
        clear_user_session(self.user_id)

        await loop.run_in_executor(None, git_push_sync, f"Add task via Discord: {title}")

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

@bot.event
async def on_ready():
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

    if is_command:
        await bot.process_commands(message)
        return

    user_id = message.author.id
    has_active_session = (get_user_session(user_id) is not None)

    if is_mentioned or is_dm or is_task_channel or has_active_session:
        clean_content = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not clean_content:
            await message.reply("👋 何か用事かな？「〜のレポート追加して」「〜終わった」「残りの課題教えて」など何でも話しかけてね！")
            return

        if clean_content.lower() in ["キャンセル", "cancel", "やめる", "とりけし", "取り消し"]:
            if has_active_session:
                clear_user_session(user_id)
                await message.reply("❌ タスクの追加をキャンセルしました。")
                return

        async with message.channel.typing():
            try:
                loop = asyncio.get_running_loop()
                api_key = CONFIG.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
                res = await loop.run_in_executor(None, analyze_dialog_with_gemini, user_id, clean_content, api_key)
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
                        return

                    user_sessions[user_id] = {"draft": draft, "updated_at": time.time()}
                    title = draft.get("title") or "無題タスク"
                    subject = draft.get("subject") or "一般"
                    category = draft.get("category", "personal")
                    priority = draft.get("priority", "medium")
                    dl = draft.get("deadline")
                    memo = draft.get("memo")

                    try:
                        dl_dt = datetime.strptime(dl, "%Y-%m-%dT%H:%M:%S")
                        dl_display = dl_dt.strftime("%Y年%m月%d日(%a) %H:%M")
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
                    embed.set_footer(text=f"AI Brain: {model_used} | 下のボタンを押して確定してください")

                    view = TaskConfirmView(user_id=user_id, draft=draft, model_used=model_used)
                    sent_msg = await message.reply(embed=embed, view=view)
                    view.message = sent_msg

                # Handle ACTION: COMPLETE
                elif action == "complete":
                    clear_user_session(user_id)
                    matched_id = res.get("matched_task_id")
                    assignments = load_assignments()
                    target = None

                    if matched_id:
                        for a in assignments:
                            if a.get("id") == matched_id:
                                target = a
                                break

                    if not target:
                        query = res.get("matched_task_title") or clean_content
                        for a in assignments:
                            if a.get("status") != "submitted" and (a.get("title") in query or query in a.get("title")):
                                target = a
                                break

                    if target:
                        target["status"] = "submitted"
                        target["completedAt"] = datetime.now(JST).isoformat()
                        save_assignments(assignments)
                        await loop.run_in_executor(None, git_push_sync, f"Complete task via Discord: {target.get('title')}")

                        embed = discord.Embed(
                            title="🎉 タスクを完了にしました！",
                            description=f"**[{target.get('subject')}]** {target.get('title')}\nお疲れ様でした！",
                            color=0xf1c40f
                        )
                        embed.set_footer(text=f"AI Brain: {model_used} | Webダッシュボードに反映済み")
                        await message.reply(embed=embed)
                    else:
                        await message.reply("🤔 完了にしたいタスクが見つかりませんでした。\n「未完了の課題を教えて」と聞いて名前を確認してみてください！")

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

                # Handle ACTION: SYNC
                elif action == "sync":
                    await message.reply("🔄 LETUSとカレンダーを同期しています...")
                    try:
                        import sync_letus
                        await loop.run_in_executor(None, sync_letus.fetch_and_sync)
                        await loop.run_in_executor(None, git_push_sync, "Auto-sync LETUS assignments via Discord")
                        await message.reply("✅ LETUSの最新課題を同期し、Webダッシュボードへ反映しました！")
                    except Exception as e:
                        await message.reply(f"⚠️ 同期中にエラーが発生しました: {e}")

                # Handle ACTION: CANCEL
                elif action == "cancel":
                    clear_user_session(user_id)
                    await message.reply("❌ タスクの追加をキャンセルしました。")

                # Handle ACTION: CHAT
                else:
                    reply_text = res.get("reply_text") or "承知いたしました！"
                    embed = discord.Embed(description=reply_text, color=0x9b59b6)
                    embed.set_footer(text=f"AI Brain: {model_used}")
                    await message.reply(embed=embed)

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
    embed.add_field(name="📋 課題を確認する", value="・「直近の締切教えて」\n・「!list」", inline=False)
    embed.add_field(name="🔄 LETUSを同期する", value="・「LETUS同期して」または「!sync」", inline=False)
    embed.add_field(name="🌐 Webダッシュボード", value="[Task Radarを開く](https://fujitomo1123581321-byte.github.io/task-radar/)", inline=False)
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        assignments = load_assignments()
        data = {
            "status": "online",
            "app": "Task Radar Discord AI Bot",
            "bot_user": bot.user.name if bot.user else "Starting...",
            "tasks_count": len(assignments),
            "timestamp": datetime.now(JST).isoformat()
        }
        resp = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(resp)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(resp)

    def do_POST(self):
        if self.path in ("/api/task-status", "/api/task-status/"):
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                payload = json.loads(body.decode("utf-8"))

                task_id = payload.get("id")
                new_status = payload.get("status")
                task_title = payload.get("title")

                if not task_id or not new_status:
                    self.send_response(400)
                    self._send_cors()
                    self.end_headers()
                    self.wfile.write(b'{"error": "Missing id or status"}')
                    return

                assignments = load_assignments()
                target = None
                for a in assignments:
                    if str(a.get("id")) == str(task_id):
                        target = a
                        break

                if not target and task_title:
                    for a in assignments:
                        if a.get("title", "").strip() == str(task_title).strip():
                            target = a
                            break

                if target:
                    target["status"] = new_status
                    if new_status == "submitted":
                        target["completedAt"] = datetime.now(JST).isoformat()
                    elif new_status == "not_started":
                        target["completedAt"] = None

                if target:
                    save_assignments(assignments)
                    threading.Thread(
                        target=git_push_sync,
                        args=(f"Update task '{target.get('title')}' status to {new_status} via Web",),
                        daemon=True
                    ).start()

                    resp = json.dumps({"status": "ok", "id": task_id, "new_status": new_status, "title": target.get("title")}, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(resp)))
                    self._send_cors()
                    self.end_headers()
                    self.wfile.write(resp)
                    print(f"🌐 [Web Sync API] Task '{target.get('title')}' status updated to {new_status}")
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

                assignments = load_assignments()
                new_assignments = [a for a in assignments if str(a.get("id")) != str(task_id)]
                save_assignments(new_assignments)

                title = payload.get("title", task_id)
                threading.Thread(
                    target=git_push_sync,
                    args=(f"Delete task '{title}' via Web",),
                    daemon=True
                ).start()

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors()
                self.end_headers()
                self.wfile.write(b'{"status": "ok", "action": "deleted"}')
                print(f"🌐 [Web Sync API] Task '{task_id}' permanently deleted via Web")
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

                assignments = load_assignments()
                new_assignments = [a for a in assignments if a.get("title") != title and str(a.get("id")) != str(task_id)]
                save_assignments(new_assignments)

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

                threading.Thread(
                    target=git_push_sync,
                    args=(f"Ignore task '{title}' via Web",),
                    daemon=True
                ).start()

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors()
                self.end_headers()
                self.wfile.write(b'{"status": "ok", "action": "ignored"}')
                print(f"🌐 [Web Sync API] Task '{title}' permanently ignored via Web")
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
    server = http.server.HTTPServer(("0.0.0.0", port), HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"🌐 [Cloud Server] Health check listener active on 0.0.0.0:{port}")

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

    # 2. Run standard Discord Bot (discord.py manages event loop & automatic backoff)
    print("Starting Discord AI Bot with standard bot.run()...")
    bot.run(token)

if __name__ == "__main__":
    main()
