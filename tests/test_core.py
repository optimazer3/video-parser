import asyncio
import re
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
    "summary": "s", "takeaways": ["t"], "ideas": ["i"],
    "videos": [{"video_number": 1, "why": "w", "hook": "h", "how_to_make": ["запись экрана"], "improve": ["x"]}],
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
    analyzer = Analyzer(llm, "high")
    report = asyncio.run(analyzer.profile_report(TIKTOK, "ai_guru", stats, {}, model="anthropic/claude-sonnet-5.5"))
    assert isinstance(report, ProfileReport) and report.videos[0].hook == "h"
    first, second = llm.payloads
    assert first["reasoning"] == {"effort": "high"} and first["model"] == "anthropic/claude-sonnet-5.5"
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "user"]


def test_analyzer_gives_up_after_two_bad_answers():
    analyzer = Analyzer(FakeLLM(["нет", "всё ещё нет"]), None)
    with pytest.raises(AnalysisError):
        asyncio.run(analyzer.single_report(make_video(1, 1000), None, model="m"))


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
        latest, popular = [make_video(1, 1000), make_video(2, 5000)], [make_video(9, 900_000)]
        assert await storage.get_profile(TIKTOK, "AI_Guru", 24) is None
        await storage.save_profile(TIKTOK, "ai_guru", latest, popular)
        cached = await storage.get_profile(TIKTOK, "AI_Guru", 24)
        disabled = await storage.get_profile(TIKTOK, "ai_guru", 0)
        # A row written by the previous version (a plain list) still loads.
        await storage.db.execute("UPDATE profiles SET data = ?", (json.dumps([v.to_dict() for v in latest]),))
        old = await storage.get_profile(TIKTOK, "ai_guru", 24)
        await storage.close()
        return cached, disabled, old

    (latest, popular), disabled, old = asyncio.run(run())
    assert [v.views for v in latest] == [1000, 5000] and popular[0].views == 900_000 and disabled is None
    assert len(old[0]) == 2 and old[1] == []


def test_hits_mix_fresh_and_all_time():
    from bot.metrics import ALL_TIME, FRESH

    latest = [make_video(i, 10_000 + i * 100) for i in range(20)]
    latest += [make_video(100, 120_000, days_ago=2), make_video(101, 70_000, days_ago=4), make_video(102, 45_000)]
    popular = [make_video(900, 3_000_000, days_ago=200), make_video(901, 1_500_000, days_ago=300),
               make_video(902, 900_000, days_ago=400), make_video(100, 120_000, days_ago=2)]
    stats = profile_stats(latest, top=5, popular=popular, now=NOW)
    assert [v.id for v in stats.hits] == ["100", "101", "102", "900", "901"]
    assert [stats.kind(v) for v in stats.hits] == [FRESH] * 3 + [ALL_TIME] * 2
    assert stats.ratio(stats.hits[3]) > 100  # measured against the latest videos' median
    assert len(stats.videos) == 23 and len(stats.all_videos) == 26  # duplicate 100 counted once
    # Without the popular list everything is fresh.
    assert all(stats.kind(v) == FRESH for v in profile_stats(latest, top=5, now=NOW).hits)


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


def test_feed_cost_usd():
    from bot.apify_source import feed_cost_usd

    # The run from the Apify bill: 6 topics x 30 videos on both platforms.
    topics = ["#нейросети", "#нейросеть", "#искусственныйинтеллект", "#chatgpt", "#aitools", "нейросети для работы"]
    assert feed_cost_usd(topics, 30, ["tiktok", "instagram"]) == pytest.approx(1.136, abs=0.01)
    assert feed_cost_usd(["#нейросети", "#chatgpt", "#aitools", "x"], 10, ["tiktok", "instagram"]) < 0.26
    assert feed_cost_usd(["#a", ""], 10, ["tiktok"]) == pytest.approx(0.038)


def test_tiktok_download_falls_back_to_apify():
    from types import SimpleNamespace as NS

    from bot.media import VideoMedia
    from bot.service import Services

    class FakeMedia:
        async def process(self, video, session, make_clip=False):
            if video.video_url:
                return VideoMedia(frames=[(0.0, b"jpeg")])
            return VideoMedia(error="yt-dlp: blocked")

    class FakeApify:
        def __init__(self):
            self.urls = []

        async def tiktok_media(self, urls):
            self.urls += urls
            return [Video(platform=TIKTOK, id="1", url=urls[0], author="a", video_url="https://api.apify.com/v.mp4")]

    svc = Services.__new__(Services)
    svc.settings, svc.media, svc.apify = NS(proxy=None), FakeMedia(), FakeApify()
    videos = [make_video(1, 1000), make_video(2, 2000)]
    media = asyncio.run(svc._process_media(videos))
    assert svc.apify.urls == [videos[0].url, videos[1].url]  # one Apify run for both
    assert media["tiktok:1"].frames and media["tiktok:2"].error  # 2 was not returned by Apify


def test_short_report_formatting():
    from bot.analyzer import SingleReport
    from bot.formatting import pack, profile_report, single_report

    report = ProfileReport.model_validate({
        "summary": "Заходят обзоры сервисов с результатом в первом кадре.",
        "takeaways": ["a", "b", "c", "d"],
        "videos": [{"video_number": 1, "why": "Готовый результат <сразу>", "hook": "Слайды в кадре",
                    "how_to_make": ["Шаг 1: запись экрана Gamma", "2) озвучка ElevenLabs", "3. субтитры CapCut",
                                    "4", "5", "6"],
                    "improve": ["x", "y", "z"]}],
        "ideas": ["i1", "i2", "i3", "i4"],
    })
    videos = [make_video(i, 10_000 + i) for i in range(8)] + [make_video(50, 100_000)]
    stats = profile_stats(videos, top=3, now=NOW)
    messages = profile_report(report, stats)
    text = "\n".join(messages)
    assert len(messages) == 1  # short blocks are packed into one message
    assert "1. запись экрана Gamma\n2. озвучка ElevenLabs\n3. субтитры CapCut" in text  # model numbering removed
    assert "6" not in text.split("Как сделать без лица")[1].split("Улучшить")[0]  # at most 5 steps
    assert "&lt;сразу&gt;" in text and "• d" not in text and "• z" not in text and "• i4" not in text
    single = single_report(SingleReport.model_validate({"video": report.videos[0].model_dump(), "ideas": []}),
                           make_video(1, 5000))
    assert "Идеи" not in single[0]
    assert pack(["a" * 3000, "b" * 3000, "c"]) == ["a" * 3000, "b" * 3000 + "\n\nc"]


def test_topics_commands():
    from bot.topics import apply_command

    current = ["#нейросети", "#chatgpt", "нейросети для работы"]
    updated, note = apply_command(current, "+ #sora, нейросети для монтажа, #ChatGPT")
    assert updated == current + ["#sora", "нейросети для монтажа"] and note == "Добавил: #sora, нейросети для монтажа"
    assert apply_command(current, "добавить #ии")[0][-1] == "#ии"
    updated, note = apply_command(current, "- chatgpt")  # no exact match: removes the hashtag
    assert updated == ["#нейросети", "нейросети для работы"] and "#chatgpt" in note
    assert apply_command(current, "-#нет такой")[1] == "Таких тем в списке нет."
    assert apply_command(["#a"], "- #a") == (["#a"], "Нельзя удалить все темы — подборке нужна хотя бы одна.")
    assert apply_command(current, "+")[0] == current
    assert apply_command(current, "reset") == (None, "Вернул темы по умолчанию.")
    assert apply_command(current, "#a, b") == (["#a", "b"], "Заменил список тем.")


def test_model_catalog_parsing():
    from bot.catalog import parse_models

    models = parse_models({"data": [
        {"id": "google/gemini-3.8-flash", "architecture": {"input_modalities": ["text", "image", "video"]},
         "top_provider": {"pricing": {"prompt_per_million": "43.8", "completion_per_million": "219.1"}}},
        {"id": "openai/gpt-6.1-terra", "architecture": {"input_modalities": ["text", "image"]},
         "top_provider": {"pricing": {"prompt_per_million": "57.2", "completion_per_million": "286.1"}}},
        {"id": "broken"},
    ]})
    gemini, terra = models["google/gemini-3.8-flash"], models["openai/gpt-6.1-terra"]
    assert gemini.video and gemini.images and not terra.video
    assert terra.estimate_rub() == pytest.approx(2.86, abs=0.01)
    assert gemini.estimate_rub(with_video=True) > gemini.estimate_rub()
    assert models["broken"].estimate_rub() is None


def test_video_mode_falls_back_to_frames():
    from bot import costs
    from bot.analyzer import SingleReport
    from bot.llm import LLMError
    from bot.media import VideoMedia

    class VideoRejectingLLM(FakeLLM):
        async def chat(self, payload):
            parts = payload["messages"][1]["content"]
            if any(p.get("type") == "video_url" for p in parts):
                self.payloads.append("video")
                raise LLMError("video_url не поддерживается", status=400)
            return await super().chat(payload)

    single = json.dumps({"video": REPORT["videos"][0], "ideas": ["i"]})
    llm = VideoRejectingLLM([single, single])
    analyzer = Analyzer(llm, "high")
    media = VideoMedia(frames=[(0.0, b"jpeg")], clip=b"mp4")

    async def run():
        spent = costs.track()
        first = await analyzer.single_report(make_video(1, 1000), media, model="google/gemini-3.8-flash", use_video=True)
        second = await analyzer.single_report(make_video(1, 1000), media, model="google/gemini-3.8-flash", use_video=True)
        return first, second, spent

    first, second, spent = asyncio.run(run())
    assert isinstance(first, SingleReport) and isinstance(second, SingleReport)
    # The video was tried once; the retry and the next report went straight to frames.
    assert llm.payloads[0] == "video" and llm.payloads.count("video") == 1
    frame_parts = llm.payloads[1]["messages"][1]["content"]
    assert any(p.get("type") == "image_url" for p in frame_parts)
    assert any("не принял видео" in note for note in spent.notes)


def test_video_mode_sends_clip():
    from bot.analyzer import _video_blocks
    from bot.media import VideoMedia

    media = VideoMedia(frames=[(0.0, b"jpeg")], clip=b"mp4")
    with_video = _video_blocks(make_video(1, 1000), 1, None, media, use_video=True)
    assert with_video[-1]["type"] == "video_url" and with_video[-1]["video_url"]["url"].startswith("data:video/mp4;base64,")
    frames = _video_blocks(make_video(1, 1000), 1, None, media, use_video=False)
    assert frames[-1]["type"] == "image_url"


def _sample_report():
    videos = [make_video(i, 10_000 + i) for i in range(8)] + [make_video(50, 100_000), make_video(51, 60_000)]
    stats = profile_stats(videos, top=3, now=NOW)
    analyses = [dict(REPORT["videos"][0], video_number=n, key_prompt="Сделай HTML-анимацию 🤯 счётчика 0→30")
                for n in (1, 2, 3)]
    report = ProfileReport.model_validate(dict(REPORT, summary="Итог " * 200, videos=analyses))
    return stats, report


def test_report_files():
    from bot.media import VideoMedia
    from bot.report import build_markdown, build_pdf, file_name, find_fonts

    stats, report = _sample_report()
    md = build_markdown(TIKTOK, "neuro.lab", stats, report, footer="Модель: m")
    assert "## Ролик №1" in md and "Сделай HTML-анимацию" in md and md.count("```") == 6
    assert file_name(TIKTOK, "neuro.lab", "pdf").startswith("razbor_neuro.lab_tiktok_")
    assert file_name(TIKTOK, "../../x", "md").startswith("razbor_.._.._x_")  # no path separators
    if find_fonts() is None:
        pytest.skip("no Cyrillic font on this machine")
    media = {v.key: VideoMedia(frames=[(0.0, b"not a jpeg")]) for v in stats.hits}  # broken frames must not break it
    pdf = build_pdf(TIKTOK, "neuro.lab", stats, report, media, footer="Модель: m")
    assert pdf[:4] == b"%PDF" and len(pdf) > 5000


def test_report_caption_and_prompt_in_chat():
    from bot import costs
    from bot.formatting import report_caption, video_analysis

    stats, report = _sample_report()
    spent = costs.Costs(apify_usd=0.11, apify_runs=[("a", 0.11)], llm_rub=2.9, llm_models=["m"],
                        notes=["🎞 нейросеть смотрела ролики целиком, со звуком"])
    caption = report_caption(TIKTOK, "neuro.lab", stats, report, spent)
    visible = re.sub(r"<[^>]+>", "", caption)
    assert len(visible) <= 1024 and "$0.11" in caption and "целиком" in caption
    chat = video_analysis(report.videos[0], None)
    assert "<code>Сделай HTML-анимацию" in chat


def test_report_marks_kinds_and_lists_everything():
    from bot.formatting import pair_analyses
    from bot.report import build_markdown

    latest = [make_video(i, 10_000 + i * 100) for i in range(10)] + [make_video(100, 120_000, days_ago=2)]
    stats = profile_stats(latest, top=3, popular=[make_video(900, 3_000_000, days_ago=200)], now=NOW)
    analyses = [dict(REPORT["videos"][0], video_number=n) for n in (7, 7, 9)]  # nonsense numbers from the model
    report = ProfileReport.model_validate(dict(REPORT, videos=analyses))
    pairs = pair_analyses(report.videos, stats.hits)
    assert [a.video_number for a, _ in pairs] == [1, 2, 3] and [v.id for _, v in pairs] == [h.id for h in stats.hits]
    md = build_markdown(TIKTOK, "neuro.lab", stats, report)
    assert "*свежий хит*" in md and "*топ за всё время*" in md and "1 самых популярных" in md
