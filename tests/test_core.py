import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.analyzer import AnalysisError, Analyzer, ProfileReport, describe_video, extract_json
from bot.apify_source import split_topics
from bot.formatting import feed_card, num, split_message
from bot.links import find_url, parse_handle, parse_url
from bot.media import frame_timestamps, vtt_to_text
from bot.metrics import pick_feed, profile_stats
from bot.models import INSTAGRAM, TIKTOK, Video, instagram_from_apify, tiktok_from_apify

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)

TIKTOK_ITEM = {
    "id": "7312345678901234567",
    "text": "Эта нейросеть делает презентацию за 30 секунд #нейросети #ai",
    "createTimeISO": "2026-10-28T10:00:00.000Z",
    "authorMeta": {"name": "ai_guru", "fans": 12000},
    "musicMeta": {"musicName": "original sound", "musicAuthor": "ai_guru", "musicOriginal": True},
    "webVideoUrl": "https://www.tiktok.com/@ai_guru/video/7312345678901234567",
    "videoMeta": {
        "duration": 34,
        "coverUrl": "https://example.com/cover.jpg",
        "subtitleLinks": [
            {"language": "eng-US", "downloadLink": "https://example.com/en.vtt"},
            {"language": "rus-RU", "downloadLink": "https://example.com/ru.vtt"},
        ],
    },
    "diggCount": 85000, "shareCount": 3400, "playCount": 1200000, "collectCount": 10000, "commentCount": 1200,
    "hashtags": [{"name": "нейросети"}, {"name": "ai"}],
    "mediaUrls": ["https://api.apify.com/v2/key-value-stores/x/records/video.mp4"],
}

INSTAGRAM_ITEM = {
    "id": "3456",
    "type": "Video",
    "shortCode": "C1abcDEF",
    "caption": "3 нейросети, которые заменят дизайнера",
    "hashtags": ["нейросети"],
    "url": "https://www.instagram.com/p/C1abcDEF/",
    "commentsCount": 300,
    "likesCount": -1,
    "videoPlayCount": 500000,
    "videoViewCount": 200000,
    "timestamp": "2026-10-25T08:00:00.000Z",
    "ownerUsername": "neuro.design",
    "videoDuration": 28.5,
    "videoUrl": "https://scontent.cdninstagram.com/v.mp4",
    "musicInfo": {"artist_name": "neuro.design", "song_name": "Original audio", "uses_original_audio": True},
}


def make_video(i: int, views: int, days_ago: float = 10, author: str = "a") -> Video:
    return Video(
        platform=TIKTOK, id=str(i), url=f"https://www.tiktok.com/@{author}/video/{i}", author=author,
        caption=f"video {i}", created_at=NOW - timedelta(days=days_ago), views=views, likes=views // 20,
        comments=views // 500, shares=views // 400, saves=views // 300,
    )


def test_tiktok_from_apify():
    video = tiktok_from_apify(TIKTOK_ITEM)
    assert video.key == "tiktok:7312345678901234567"
    assert video.author == "ai_guru"
    assert video.views == 1_200_000 and video.saves == 10_000
    assert video.subtitle_url == "https://example.com/ru.vtt"
    assert video.video_url.endswith("video.mp4")
    assert video.hashtags == ["нейросети", "ai"]
    assert video.created_at == datetime(2026, 10, 28, 10, tzinfo=timezone.utc)
    assert "оригинальный звук" in video.music
    assert tiktok_from_apify({"error": "Post not found"}) is None
    assert tiktok_from_apify({**TIKTOK_ITEM, "isSlideshow": True}) is None


def test_instagram_from_apify():
    video = instagram_from_apify(INSTAGRAM_ITEM)
    assert video.platform == INSTAGRAM
    assert video.views == 500_000  # plays win over legacy views
    assert video.likes == 0  # hidden likes (-1) are treated as unknown
    assert video.author == "neuro.design"
    assert instagram_from_apify({**INSTAGRAM_ITEM, "type": "Image", "productType": "feed", "videoUrl": None}) is None


def test_video_roundtrip():
    video = tiktok_from_apify(TIKTOK_ITEM)
    assert Video.from_dict(video.to_dict()) == video


def test_parse_links():
    assert parse_url("https://www.tiktok.com/@ai_guru?lang=ru").username == "ai_guru"
    target = parse_url("https://www.tiktok.com/@ai_guru/video/7312345678901234567?is_from_webapp=1")
    assert (target.kind, target.url) == ("video", "https://www.tiktok.com/@ai_guru/video/7312345678901234567")
    assert parse_url("https://instagram.com/neuro.design/").username == "neuro.design"
    reel = parse_url("https://www.instagram.com/reel/C1abcDEF/?igsh=xyz")
    assert (reel.platform, reel.kind, reel.url) == (INSTAGRAM, "video", "https://www.instagram.com/reel/C1abcDEF/")
    assert parse_url("https://www.instagram.com/explore/tags/ai/") is None
    assert find_url("глянь instagram.com/neuro.design плиз") == "https://instagram.com/neuro.design"
    assert find_url("вот: https://vm.tiktok.com/ZMabc123/.") == "https://vm.tiktok.com/ZMabc123/"
    assert parse_handle("@ai_guru") == "ai_guru"
    assert parse_handle("привет") is None


def test_split_topics():
    assert split_topics(["#нейросети", "chat gpt лайфхаки", " #ai tools "]) == (
        ["нейросети", "aitools"], ["chat gpt лайфхаки"],
    )


def test_profile_stats_finds_hits_and_flops():
    videos = [make_video(i, 10_000 + i * 100) for i in range(10)]
    videos += [make_video(100, 90_000), make_video(101, 55_000), make_video(102, 1_000), make_video(103, 500, days_ago=1)]
    stats = profile_stats(videos, top=5, now=NOW)
    assert [v.id for v in stats.hits[:2]] == ["100", "101"]
    assert stats.hit_count == 2
    assert len(stats.hits) == 3  # at least three videos are always analysed
    assert stats.flops[0].id == "102"
    assert "103" not in {v.id for v in stats.flops}  # too fresh to be called a flop
    assert stats.ratio(stats.hits[0]) > 8


def test_pick_feed_filters_and_ranks():
    videos = [
        make_video(1, 500_000, days_ago=1, author="x"),
        make_video(2, 50_000, days_ago=2, author="y"),
        make_video(3, 5_000, days_ago=1, author="z"),  # too few views
        make_video(4, 900_000, days_ago=30, author="w"),  # too old
        make_video(5, 400_000, days_ago=1, author="x"),
        make_video(6, 300_000, days_ago=1, author="x"),  # third video of the same author
        make_video(7, 600_000, days_ago=1, author="seen"),
    ]
    picked = pick_feed(videos, seen={"tiktok:7"}, days=7, min_views=20_000, min_likes=500, limit=10, now=NOW)
    assert [v.id for v in picked] == ["1", "5", "2"]


def test_frame_timestamps():
    assert frame_timestamps(30, 6) == [0.0, 1.0, 2.5, 11.8, 20.7, 29.5]
    assert frame_timestamps(None, 6) == [0.0, 1.0, 2.5]
    short = frame_timestamps(2, 6)
    assert short and max(short) < 2


def test_vtt_to_text():
    raw = "WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\nПривет, это <c>нейросеть</c>\n\n2\n00:00:02.000 --> 00:00:03.000\nПривет, это нейросеть\nкоторая всё делает\n"
    assert vtt_to_text(raw) == "Привет, это нейросеть которая всё делает"


def test_formatting():
    assert num(1_234_567) == "1.2M" and num(85_000) == "85K" and num(999) == "999"
    card = feed_card(tiktok_from_apify(TIKTOK_ITEM), 1)
    assert "@ai_guru" in card and "1.2M" in card and "ER" in card
    text = "\n".join(["строка " + "x" * 50] * 200)
    chunks = split_message(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks) and "\n".join(chunks) == text
    assert all(len(c) <= 100 for c in split_message("слово " * 100 + "&amp;" * 30, limit=100))


REPORT = {
    "summary": "s", "hit_patterns": ["p"], "weak_spots": [], "recommendations": ["r"], "content_ideas": ["i"],
    "videos": [{"video_number": 1, "why_it_worked": "w", "hook": "h", "structure": "st",
                "what_to_improve": ["x"], "repeat_this": ["y"]}],
}


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.payloads = []

    async def chat(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        return self.replies.pop(0), "stop"


def test_extract_json():
    assert extract_json('```json\n{"a": {"b": 1}}\n```') == {"a": {"b": 1}}
    assert extract_json('Вот разбор:\n{"a": 1}\nУдачи!') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("без json")


def test_analyzer_retries_bad_json():
    videos = [make_video(i, 10_000 + i) for i in range(8)] + [make_video(50, 100_000)]
    stats = profile_stats(videos, top=3, now=NOW)
    llm = FakeLLM(['{"summary": "обрезано"', "```json\n" + json.dumps(REPORT) + "\n```"])
    analyzer = Analyzer(llm, "anthropic/claude-sonnet-5.5", "high")
    report = asyncio.run(analyzer.profile_report(TIKTOK, "ai_guru", stats, {}))
    assert isinstance(report, ProfileReport) and report.videos[0].hook == "h"
    first, second = llm.payloads
    assert first["reasoning"] == {"effort": "high"} and first["model"] == "anthropic/claude-sonnet-5.5"
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "user"]


def test_analyzer_gives_up_after_two_bad_answers():
    analyzer = Analyzer(FakeLLM(["нет", "всё ещё нет"]), "m", None)
    with pytest.raises(AnalysisError):
        asyncio.run(analyzer.single_report(make_video(1, 1000), None))


def test_describe_video():
    prompt = describe_video(tiktok_from_apify(TIKTOK_ITEM), 1, 4.2)
    assert "×4.2" in prompt and "сохранения: 10 000" in prompt


def test_run_cost_usd():
    from types import SimpleNamespace as NS

    from bot.apify_source import run_cost_usd

    ppe = NS(
        pricing_info=NS(pricing_model="PAY_PER_EVENT", pricing_per_event=NS(actor_charge_events={
            "actor-start": NS(event_price_usd=0.005), "result": NS(event_price_usd=0.003),
            "video-download": NS(event_price_usd=0.02),
        })),
        charged_event_counts={"actor-start": 1, "result": 30, "video-download": 5},
        platform_usage_billing_model="DEVELOPER", usage_total_usd=0.4,
    )
    assert run_cost_usd(ppe, 30) == pytest.approx(0.005 + 0.09 + 0.1)
    per_item = NS(pricing_info=NS(pricing_model="PRICE_PER_DATASET_ITEM", price_per_unit_usd=0.0026),
                  charged_event_counts=None, platform_usage_billing_model=None, usage_total_usd=0.2)
    assert run_cost_usd(per_item, 30) == pytest.approx(0.078)
    free = NS(pricing_info=None, charged_event_counts=None, platform_usage_billing_model=None, usage_total_usd=0.12)
    assert run_cost_usd(free, 100) == pytest.approx(0.12)


def test_costs_are_tracked_across_tasks():
    from bot import costs
    from bot.formatting import costs_line

    async def job():
        spent = costs.track()
        await asyncio.gather(asyncio.sleep(0, costs.add_apify("a", 0.1)), asyncio.sleep(0, costs.add_apify("b", 0.05)))

        async def llm():
            costs.add_llm("openai/gpt-6.1-terra", 2.5)
            costs.add_llm("openai/gpt-4o-mini-transcribe", 0.3)

        await asyncio.gather(llm())
        return spent

    spent = asyncio.run(job())
    assert spent.apify_usd == pytest.approx(0.15) and len(spent.apify_runs) == 2
    line = costs_line(spent)
    assert "$0.15" in line and "2.8 ₽" in line and "openai/gpt-6.1-terra" in line
    costs.add_apify("outside", 1.0)  # no tracker in this context: ignored, no error


def test_duplicate_env_keys(tmp_path):
    from bot.config import duplicate_keys

    env = tmp_path / ".env"
    env.write_text("LLM_MODEL=openai/gpt-6.1-terra\n# LLM_MODEL=x\nAPIFY_TOKEN=1\nLLM_MODEL=anthropic/claude-sonnet-5.5\n",
                   encoding="utf-8")
    assert duplicate_keys(str(env)) == ["LLM_MODEL"]
    assert duplicate_keys(str(tmp_path / "missing")) == []


def test_profile_cache(tmp_path):
    from bot.storage import Storage

    async def run():
        storage = Storage(str(tmp_path / "db.sqlite3"))
        await storage.open()
        videos = [make_video(1, 1000), make_video(2, 5000)]
        assert await storage.get_profile(TIKTOK, "AI_Guru", 24) is None
        await storage.save_profile(TIKTOK, "ai_guru", videos)
        cached = await storage.get_profile(TIKTOK, "AI_Guru", 24)
        disabled = await storage.get_profile(TIKTOK, "ai_guru", 0)
        await storage.close()
        return cached, disabled

    cached, disabled = asyncio.run(run())
    assert [v.views for v in cached] == [1000, 5000] and disabled is None


def test_tiktok_from_ytdlp():
    from bot.models import tiktok_from_ytdlp

    video = tiktok_from_ytdlp({
        "id": "7312", "uploader": "ai_guru", "description": "Нейросеть для презентаций #нейросети #ai",
        "timestamp": 1761800000, "duration": 34, "view_count": 120000, "like_count": 9000, "comment_count": 300,
        "repost_count": 150, "webpage_url": "https://www.tiktok.com/@ai_guru/video/7312", "track": "original sound",
        "artists": ["ai_guru"], "thumbnail": "https://example.com/c.jpg",
    })
    assert (video.key, video.author, video.views, video.shares) == ("tiktok:7312", "ai_guru", 120000, 150)
    assert video.hashtags == ["нейросети", "ai"] and video.music == "ai_guru — original sound"
    assert video.saves is None and video.created_at.year == 2025
