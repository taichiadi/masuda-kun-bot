import os
import hashlib
import hmac
import base64
from flask import Flask, request, abort
import anthropic
import requests
from supabase import create_client
from tavily import TavilyClient

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
BOT_NAME = "@増田くん"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY)

SYSTEM_PROMPT = """あなたは「増田くん」という名前の塾のAIアシスタントです。
増田塾のスタッフが使うLINEグループで、スタッフをサポートします。

以下の機能を自然な会話で提供してください：

【タスク管理】
- タスクの追加・確認・完了を管理
- 「〇〇しないと」「〇〇お願い」などの言葉からタスクを読み取る

【施策相談】
- 塾の運営や指導方針についての相談に乗る
- 建設的なフィードバックをする

【シフト管理】
- シフトの確認・調整をサポート

【事務連絡】
- お知らせや連絡事項を整理・共有

【生徒対応Q&A】
- 過去の対応事例から適切な対応方法を提案
- 「こういう状況でどうすれば？」という質問に過去事例から回答

日本語で簡潔に回答してください。フレンドリーで頼りになる先輩スタッフのような口調で。"""


@app.route("/")
def health():
    return "OK"


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_value).decode("utf-8")

    if not hmac.compare_digest(signature, expected):
        abort(400)

    data = request.json
    for event in data.get("events", []):
        if event["type"] == "message" and event["message"]["type"] == "text":
            handle_message(event)

    return "OK"


def get_history(chat_id):
    result = supabase.table("masuda_conversations")\
        .select("role,content")\
        .eq("chat_id", chat_id)\
        .order("created_at")\
        .limit(20)\
        .execute()
    return [{"role": r["role"], "content": r["content"]} for r in result.data]


def save_message(chat_id, role, content):
    supabase.table("masuda_conversations").insert({
        "chat_id": chat_id,
        "role": role,
        "content": content
    }).execute()


def search_qa(query):
    result = supabase.table("masuda_qa")\
        .select("situation,response")\
        .execute()
    if not result.data:
        return ""
    cases = "\n".join([f"状況: {r['situation']}\n対応: {r['response']}" for r in result.data[-10:]])
    return f"\n\n【過去の対応事例】\n{cases}"


def handle_message(event):
    user_message = event["message"]["text"]
    source_type = event["source"]["type"]

       # @増田くんへのメンションのみ反応
    if BOT_NAME not in user_message:
        return
    user_message = user_message.replace(BOT_NAME, "").strip()


    reply_token = event["replyToken"]
    chat_id = event["source"].get("groupId") or event["source"].get("userId")

    # Q&A検索キーワード
    qa_context = ""
    if any(kw in user_message for kw in ["どうすれば", "どう対応", "困って", "対応方法"]):
        qa_context = search_qa(user_message)

    # 市場調査キーワード
    search_context = ""
    if any(kw in user_message for kw in ["調べて", "検索", "最新", "トレンド"]):
        try:
            result = tavily.search(query=user_message, max_results=3)
            search_context = "\n\n【検索結果】\n"
            for r in result.get("results", []):
                search_context += f"- {r['title']}: {r['content'][:200]}\n"
        except Exception as e:
            print(f"Tavily検索エラー: {e}")

    save_message(chat_id, "user", user_message)
    history = get_history(chat_id)

    if qa_context or search_context:
        history[-1]["content"] += qa_context + search_context

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=history
        )
        ai_reply = response.content[0].text
    except Exception as e:
        print(f"Claude APIエラー: {e}")
        ai_reply = f"エラーが発生しました: {e}"

    save_message(chat_id, "assistant", ai_reply)

    result = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": ai_reply}]
        }
    )
    print(f"LINE返信結果: {result.status_code} {result.text}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
