"""
Discord AI Task Bot for Tokyo University of Science (TUS)
Powered by Google Gemini (Auto-fallback: 3.8 -> 3.7 -> 3.6 -> 3.5 -> 2.5) & discord.py
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
import asyncio
import subprocess
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

import discord
from discord.ext import commands

# -------------------------------------------------------------
# Configuration & Paths
# -------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
JSON_PATH = os.path.join(BASE_DIR, "assignments.json")
JS_PATH = os.path.join(BASE_DIR, "assignments.js")

JST = timezone(timedelta(hours=9))

def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Config file not found at: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def load_assignments():
    if not os.path.exists(JSON_PATH):
        return []
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_assignments(items):
    # Save assignments.json
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    # Save assignments.js
    with open(JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.INITIAL_ASSIGNMENTS = " + json.dumps(items, ensure_ascii=False, indent=2) + ";\n")

def git_push_sync(commit_msg="Update assignments via Discord Bot"):
    """Sync changes to GitHub in background"""
    try:
        subprocess.run(["git", "add", "assignments.json", "assignments.js"], cwd=BASE_DIR, check=True, capture_output=True)
        # Check if there are changes
        status = subprocess.run(["git", "status", "--porcelain"], cwd=BASE_DIR, capture_output=True, text=True)
        if "assignments.json" in status.stdout or "assignments.js" in status.stdout:
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=BASE_DIR, check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR, check=True, capture_output=True)
            print(f"[Git Sync] Successfully pushed to GitHub: {commit_msg}")
            return True
        return False
    except Exception as e:
        print(f"[Git Sync Error] {e}")
        return False

# -------------------------------------------------------------
# Gemini AI Engine with Dynamic Fallback
# -------------------------------------------------------------
GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash"
]

def call_gemini(prompt: str, api_key: str):
    """Calls Gemini API with automatic fallback through the model hierarchy."""
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
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
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
    """Extract and parse JSON from AI response safely."""
    # Look for code block ```json ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", ai_text)
    raw = m.group(1).strip() if m else ai_text.strip()
    
    # Try direct parse
    try:
        return json.loads(raw)
    except Exception:
        # Try to find { ... }
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw[start:end+1])
        raise ValueError("Could not parse JSON from AI output.")

# -------------------------------------------------------------
# Intent Processing Logic
# -------------------------------------------------------------
def analyze_intent_with_gemini(user_msg: str, api_key: str):
    assignments = load_assignments()
    now_dt = datetime.now(JST)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S (JST, %A)")

    # Build active (incomplete) tasks summary for context
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
ユーザーからのメッセージを自然言語解析し、以下のアクション（add, complete, list, sync, chat）のいずれかを決定してください。

【現在の未完了タスク一覧】:
{json.dumps(tasks_summary, ensure_ascii=False, indent=2)}

【アクション定義】:
1. "add": 新しい課題やタスクの追加
   - 例: 「明日の18時までに電磁気のレポート提出を追加して」「金曜夜バイトシフト」「10/25にTOEIC」
   - 必要な情報: title, subject (科目または分類), deadline (YYYY-MM-DDTHH:MM:SS), category ('academic' または 'personal'), priority ('high' | 'medium' | 'low')
   - deadlineはユーザーの指示（明日、来週金曜など）から現在の {now_str} を基準に正確に計算してください。時間が省略されている場合は23:59:00または18:00:00等適切な時刻を設定。
2. "complete": 既存タスクの完了
   - 例: 「ミニテスト4終わった」「電磁気レポート完了にした」「定期券更新やったよ」
   - 上記の【現在の未完了タスク一覧】から、該当する最も一致するタスクの 'matched_task_id' を特定してください。
3. "list": 直近の課題一覧や残りの確認
   - 例: 「直近の締切教えて」「残りの課題なにがある？」「今週の予定」
4. "sync": LETUSとの手動同期依頼
   - 例: 「LETUS同期して」「課題を最新に更新して」「sync」
5. "chat": 質問、勉強の相談、雑談
   - 例: 「電磁気学ってどう勉強すればいい？」「お疲れ様」

【出力フォーマット】:
必ず以下のキーを持つJSONのみを出力してください（マークダウンの ```json ... ``` で囲んでください）。
```json
{{
  "action": "add" | "complete" | "list" | "sync" | "chat",
  "title": "課題・タスクのタイトル（addの場合）",
  "subject": "科目または分類（例: 電磁気学, 手続き, アルバイト等）",
  "deadline": "YYYY-MM-DDTHH:MM:SS（addの場合）",
  "category": "academic" | "personal",
  "priority": "high" | "medium" | "low",
  "matched_task_id": "一致したタスクのID（completeの場合）",
  "matched_task_title": "一致したタスクのタイトル（completeの場合）",
  "reply_text": "ユーザーへの分かりやすく親切な日本語メッセージ"
}}
```

ユーザーのメッセージ: 「{user_msg}」
"""
    ai_raw, model_used = call_gemini(prompt, api_key)
    parsed = parse_json_from_ai(ai_raw)
    parsed["_model_used"] = model_used
    return parsed

# -------------------------------------------------------------
# Discord Bot Setup
# -------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True  # Required for reading chat messages

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
CONFIG = load_config()

@bot.event
async def on_ready():
    print(f"==================================================")
    print(f"🤖 Bot is ONLINE: {bot.user.name} (ID: {bot.user.id})")
    print(f"🏠 Connected Guilds: {len(bot.guilds)}")
    for g in bot.guilds:
        print(f"   - {g.name} (ID: {g.id})")
    print(f"🧠 AI Engine: Google Gemini (Active Model: {GEMINI_MODELS[0]} with Fallbacks)")
    print(f"==================================================")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="課題と締切 | 話しかけてタスク追加"
        )
    )

@bot.event
async def on_message(message: discord.Message):
    # Ignore bot's own messages
    if message.author == bot.user:
        return

    # Check if bot should respond:
    # 1. Mentioned (@bot)
    # 2. Direct Message (DM)
    # 3. Message in a channel that starts with '!' command
    is_mentioned = bot.user in message.mentions
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_command = message.content.startswith("!")
    is_task_channel = False
    if hasattr(message.channel, "name"):
        ch_name = str(message.channel.name).lower()
        if any(k in ch_name for k in ["課題", "タスク", "task", "todo", "bot"]):
            is_task_channel = True

    # Process traditional !commands if typed
    if is_command:
        await bot.process_commands(message)
        return

    # If mentioned, in DM, or in dedicated task channel, handle with natural language AI
    if is_mentioned or is_dm or is_task_channel:
        clean_content = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not clean_content:
            await message.reply("👋 何か用事かな？「〜のレポート追加して」「〜終わった」「残りの課題教えて」など何でも話しかけてね！")
            return

        async with message.channel.typing():
            try:
                loop = asyncio.get_running_loop()
                # Run Gemini parsing in thread pool to avoid blocking Discord heartbeat
                res = await loop.run_in_executor(None, analyze_intent_with_gemini, clean_content, CONFIG["gemini_api_key"])
                action = res.get("action", "chat")
                model_used = res.get("_model_used", "Gemini")

                # Handle ACTION: ADD
                if action == "add":
                    title = res.get("title") or clean_content
                    subject = res.get("subject") or "一般"
                    category = res.get("category", "personal")
                    priority = res.get("priority", "medium")
                    dl = res.get("deadline")
                    
                    if not dl:
                        # Default to tomorrow 23:59:00 if not deduced
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
                        "memo": f"Discordより登録: {clean_content}",
                        "folderPath": None,
                        "createdAt": datetime.now(JST).isoformat(),
                        "completedAt": None
                    }

                    assignments = load_assignments()
                    assignments.append(new_item)
                    save_assignments(assignments)
                    
                    # Push to GitHub
                    await loop.run_in_executor(None, git_push_sync, f"Add task via Discord: {title}")

                    # Format Deadline string nicely
                    try:
                        dl_dt = datetime.strptime(dl, "%Y-%m-%dT%H:%M:%S")
                        dl_display = dl_dt.strftime("%Y年%m月%d日 %H:%M")
                    except Exception:
                        dl_display = dl

                    embed = discord.Embed(
                        title="✅ タスクを追加しました！",
                        description=f"**[{subject}]** {title}",
                        color=0x2ecc71
                    )
                    embed.add_field(name="⏰ 締切", value=dl_display, inline=True)
                    embed.add_field(name="🏷️ 分類", value="📚 学業" if category == "academic" else "👤 自分", inline=True)
                    embed.add_field(name="🔥 優先度", value=priority.upper(), inline=True)
                    embed.add_field(name="🌐 Webダッシュボード", value="[Task Radarを開く](https://fujitomo1123581321-byte.github.io/task-radar/)", inline=False)
                    embed.set_footer(text=f"AI Brain: {model_used} | Task ID: {new_id}")
                    await message.reply(embed=embed)

                # Handle ACTION: COMPLETE
                elif action == "complete":
                    matched_id = res.get("matched_task_id")
                    assignments = load_assignments()
                    target = None

                    if matched_id:
                        for a in assignments:
                            if a.get("id") == matched_id:
                                target = a
                                break

                    # If not matched by ID, try title fuzzy search
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
                        await message.reply(f"🤔 完了にしたいタスクが見つかりませんでした。\n「未完了の課題を教えて」と聞いて名前を確認してみてください！")

                # Handle ACTION: LIST
                elif action == "list":
                    assignments = load_assignments()
                    uncompleted = [a for a in assignments if a.get("status") != "submitted"]
                    
                    # Sort by deadline
                    def parse_dl(item):
                        try:
                            return datetime.strptime(item.get("deadline", "9999-12-31T23:59:00"), "%Y-%m-%dT%H:%M:%S")
                        except Exception:
                            return datetime.max
                    
                    uncompleted.sort(key=parse_dl)
                    now_dt = datetime.now(JST).replace(tzinfo=None)

                    embed = discord.Embed(
                        title=f"📋 未完了の課題・タスク一覧（残り {len(uncompleted)} 件）",
                        color=0x3498db
                    )
                    
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
                            lines.append(f"**{idx}. [{subject}] {title}**\n   締切: {dl_formatted} ({remaining_txt})")

                        embed.description = "\n\n".join(lines)
                        if len(uncompleted) > 8:
                            embed.set_footer(text=f"他 {len(uncompleted) - 8} 件のタスクがあります | 全体はWebダッシュボードで確認できます")

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

                # Handle ACTION: CHAT
                else:
                    reply_text = res.get("reply_text") or "承知いたしました！"
                    embed = discord.Embed(
                        description=reply_text,
                        color=0x9b59b6
                    )
                    embed.set_footer(text=f"AI Brain: {model_used}")
                    await message.reply(embed=embed)

            except Exception as e:
                print(f"[Error in message handler]: {e}")
                await message.reply(f"⚠️ 処理中にエラーが発生しました: {e}")

# -------------------------------------------------------------
# Slash / Text Commands for Quick Access
# -------------------------------------------------------------
@bot.command(name="help")
async def cmd_help(ctx):
    embed = discord.Embed(
        title="🤖 Task Radar AI Bot の使い方",
        description="自然言語で話しかけるだけで、課題の追加・完了・確認ができます！\nメンション（`@fujitomo bot ai`）またはDMで話しかけてください。",
        color=0x3498db
    )
    embed.add_field(
        name="📝 タスクを追加する",
        value="・「明日の18時までに電磁気のレポート提出を追加して」\n・「10/25にTOEICテスト」\n・「金曜夜にバイトシフト提出」",
        inline=False
    )
    embed.add_field(
        name="✅ タスクを完了にする",
        value="・「ミニテスト4終わったよ」\n・「電磁気レポート完了」\n・「定期券の更新やった」",
        inline=False
    )
    embed.add_field(
        name="📋 課題を確認する",
        value="・「直近の締切教えて」\n・「残りの課題何がある？」\n・「!list」コマンドでもOK",
        inline=False
    )
    embed.add_field(
        name="🔄 LETUSを同期する",
        value="・「LETUS同期して」または「!sync」",
        inline=False
    )
    embed.add_field(
        name="🌐 Webダッシュボード",
        value="[Task Radarを開く](https://fujitomo1123581321-byte.github.io/task-radar/)",
        inline=False
    )
    embed.set_footer(text="Google Gemini 3.8/3.7/3.6/3.5 自動フォールバック搭載")
    await ctx.send(embed=embed)

@bot.command(name="list")
async def cmd_list(ctx):
    # Delegate to the list flow
    class FakeMsg:
        author = ctx.author
        content = "!list"
        mentions = [bot.user]
        channel = ctx.channel
        reply = ctx.send
    # Reuse list logic simply
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
# Main Runner
# -------------------------------------------------------------
def main():
    token = CONFIG.get("discord_bot_token")
    if not token:
        print("Error: 'discord_bot_token' is missing in config.json")
        sys.exit(1)
    
    print("Starting Discord AI Bot...")
    bot.run(token)

if __name__ == "__main__":
    main()
