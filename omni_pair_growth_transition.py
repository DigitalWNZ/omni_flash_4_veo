import base64
import mimetypes
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import google.auth
from google import genai
from google.genai import types
from google.cloud import storage

# 默认配置 (使用 veo-testing 项目，已放宽部分安全过滤策略)
PROJECT_ID = "veo-testing"
LOCATION = "global"
MODEL_ID = "gemini-omni-1.1-flash-preview"
API_REVISION = "2026-05-20"

# 默认输入的 3 组首尾帧图片 (每组 2 张: [首帧 URI, 尾帧 URI])
DEFAULT_IMAGE_PAIRS: List[Dict[str, str]] = [
    {
        "name": "女生成长4",
        "start_uri": "gs://0---agolis-allen-first/honor-omni-4-veo/女生成长4-1.jpg",
        "end_uri": "gs://0---agolis-allen-first/honor-omni-4-veo/女生成长4-2.jpg",
    },
    {
        "name": "男生成长1",
        "start_uri": "gs://0---agolis-allen-first/honor-omni-4-veo/男生成长1-1.jpg",
        "end_uri": "gs://0---agolis-allen-first/honor-omni-4-veo/男生成长1-2.jpg",
    },
    {
        "name": "男生成长2",
        "start_uri": "gs://0---agolis-allen-first/honor-omni-4-veo/男生成长2-1.jpg",
        "end_uri": "gs://0---agolis-allen-first/honor-omni-4-veo/男生成长2-2.jpg",
    },
]

# 通用首尾帧平滑过渡提示词：融合动作设计、穿搭流动过渡与浅弧度运镜/时空背景过渡经验
UNIVERSAL_PAIR_TRANSITION_PROMPT = """You are an expert cinematic director. Generate a single, continuous, seamless video that transitions naturally from the first image (start frame, Image A) to the second image (end frame, Image B), strictly avoiding any abrupt hard cuts or static morphs.

Apply these three core transition techniques in one fluid motion:
1. **Design the Movement (Continuous Turn or Walk):** Create a smooth, natural sequence of actions that bridges the subject's pose, gesture, and positioning from the first image to the second. The movement MUST include a clear midpoint where the subject's face is turned away from the camera (such as a graceful spin/pirouette, turning the body while walking left or right, or looking away in profile) so the facial and age progression happens naturally during the turn.
2. **Flowing Outfit Transition:** Complete the visual evolution into the second image's style during the active movement—having the new outer layer smoothly flow over from the shoulders or blend seamlessly while the subject is mid-turn or mid-stride.
3. **Background & Camera Transition:** Synchronize the environmental shift with the movement:
   - **If spinning/turning:** Use a smooth time-lapse flow effect in the background as the camera orbits gently.
   - **If walking/shifting left or right:** Use a **shallow horizontal arc** camera movement starting from a stable horizontal tracking shot, creating a smooth perspective shift where the background of Image A slowly glides out while the background of Image B naturally slides in.
Ensure the final frames settle smoothly into the exact composition, lighting, and pose of Image B."""

# 当输入包含幼童照片且触发输出端提示词重述建议 (Try rephrasing the prompt) 时的温情成长运镜提示词
SAFE_GROWTH_FALLBACK_PROMPT = """You are a cinematic director. Create a heartwarming, continuous one-shot growth transition video from the first photo (start frame, Image A) to the second photo (end frame, Image B).
1. **Movement:** Have the subject perform a natural, fluid action (such as walking sideways or turning around in a gentle spin) that includes a moment where their face is turned away from the camera, allowing a seamless transition in age, hair, and style.
2. **Camera & Background:** Use a smooth shallow horizontal arc camera movement (or time-lapse orbit during a turn) so the background of Image A gently glides out while the background and outfit of Image B smoothly slide in, settling naturally into the exact pose of the second photo without any hard cut."""

# 新版对比提示词（去掉“必须包含人物面部短暂不直接正对镜头的瞬间”的限制，保留原有提示词不覆盖）
UNIVERSAL_PAIR_TRANSITION_PROMPT_NO_FACE_AWAY = """You are an expert cinematic director. Generate a single, continuous, seamless video that transitions naturally from the first image (start frame, Image A) to the second image (end frame, Image B), strictly avoiding any abrupt hard cuts.

Apply these three core transition techniques in one fluid motion:
1. **Design the Movement:** Design a natural sequence of actions that allows the subject's pose, gesture, expression, and positioning to transition smoothly and naturally from the first image to the second.
2. **Flowing Outfit Transition:** Complete the visual evolution into the second image's style during the active movement—having the new outer layer smoothly flow over from the shoulders or morph naturally into the new outfit while the subject moves.
3. **Background & Camera Transition:** Synchronize the environmental shift with the movement:
   - **If turning or moving in place:** Use a smooth time-lapse flow effect in the background as time progresses.
   - **If walking or shifting left/right:** Use a **shallow horizontal arc** camera movement starting from a stable horizontal tracking shot, creating a smooth perspective shift where the background of Image A slowly glides out while the background of Image B naturally slides in.
Ensure the final frames settle smoothly into the exact composition, lighting, and pose of Image B."""

SAFE_GROWTH_FALLBACK_PROMPT_NO_FACE_AWAY = """You are a cinematic director. Create a heartwarming, continuous one-shot growth transition video from the first photo (start frame, Image A) to the second photo (end frame, Image B).
1. **Movement:** Have the subject perform a natural, fluid action that smoothly bridges their pose, expression, age, hair, and style from the first photo to the second photo.
2. **Camera & Background:** Use a smooth shallow horizontal arc camera movement or gentle time-lapse flow so the background of Image A gently glides out while the background and outfit of Image B smoothly slide in, settling naturally into the exact pose of the second photo without any hard cut."""


def _get_field(obj, key: str, default=None):
    """兼容 dict 和 SDK 对象属性读取。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def get_storage_client() -> storage.Client:
    """获取兼容本地 ADC 与 Colab 环境的 GCS Client。"""
    try:
        creds, _ = google.auth.default()
        if hasattr(creds, "with_quota_project"):
            creds = creds.with_quota_project(None)
        return storage.Client(project=None, credentials=creds)
    except Exception:
        return storage.Client()


def guess_mime_type(uri: str) -> str:
    """根据 URI 后缀推断图片 MIME 类型。"""
    mime, _ = mimetypes.guess_type(uri)
    return mime or "image/jpeg"


def transfer_gcs_movie_to_local(
    gcs_uri: str,
    local_dir: str = ".",
    local_filename: Optional[str] = None,
) -> str:
    """将 GCS 上的视频文件 (gs://...) 下载到本地目录，方便本地 Debug 与查看。"""
    if not gcs_uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI (must start with gs://): {gcs_uri}")

    bucket_name, blob_name = gcs_uri[len("gs://") :].split("/", 1)
    out_dir = Path(local_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not local_filename:
        local_filename = Path(blob_name).name or "debug_gcs_movie.mp4"
        if not local_filename.endswith(".mp4"):
            local_filename += ".mp4"

    local_path = out_dir / local_filename
    print(f"[Debug GCS -> Local] 正在从 {gcs_uri} 下载视频到本地: {local_path} ...")
    client = get_storage_client()
    blob = client.bucket(bucket_name).blob(blob_name)
    blob.download_to_filename(str(local_path))
    size_mb = local_path.stat().st_size / (1024 * 1024)
    print(f"[Debug GCS -> Local] 下载完成 ({size_mb:.2f} MB): {local_path}")
    return str(local_path)


def _extract_gcs_uris(obj) -> List[str]:
    """递归提取任意 step 结构中出现的 gs:// URI，便于 Debug 追踪。"""
    found: List[str] = []
    if isinstance(obj, str):
        if obj.startswith("gs://"):
            found.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            found.extend(_extract_gcs_uris(v))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found.extend(_extract_gcs_uris(item))
    elif hasattr(obj, "__dict__"):
        for v in vars(obj).values():
            found.extend(_extract_gcs_uris(v))
    return found


def build_pair_interaction_input(
    start_uri: str,
    end_uri: str,
    prompt: str = UNIVERSAL_PAIR_TRANSITION_PROMPT,
) -> list:
    """组装单组首尾帧（2张图片 + 通用平滑过渡提示词）的输入。"""
    content = [
        {
            "type": "image",
            "mime_type": guess_mime_type(start_uri),
            "uri": start_uri,
        },
        {
            "type": "image",
            "mime_type": guess_mime_type(end_uri),
            "uri": end_uri,
        },
        {
            "type": "text",
            "text": prompt,
        },
    ]
    return [
        {
            "type": "user_input",
            "content": content,
        }
    ]


def generate_single_pair_video(
    client: genai.Client,
    pair_name: str,
    start_uri: str,
    end_uri: str,
    prompt: str = UNIVERSAL_PAIR_TRANSITION_PROMPT,
    fallback_prompt: str = SAFE_GROWTH_FALLBACK_PROMPT,
    output_dir: str = ".",
    file_suffix: str = "",
    debug_save_all_gcs_videos: bool = True,
) -> Optional[str]:
    """针对单组首尾帧图片调用 gemini-omni-1.1-flash-preview 生成过渡视频。"""
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    print(f"\n============================================================")
    print(f"正在生成组 [{pair_name}{file_suffix}] 的首尾帧过渡视频 ...")
    print(f"- 首帧 (Image A): {start_uri}")
    print(f"- 尾帧 (Image B): {end_uri}")

    try:
        interaction = client.interactions.create(
            model=MODEL_ID,
            input=build_pair_interaction_input(
                start_uri=start_uri,
                end_uri=end_uri,
                prompt=prompt,
            ),
            timeout=1200.0,
        )
    except Exception as e:
        err_str = str(e)
        if prompt != fallback_prompt and (
            "minor safety" in err_str
            or "content_blocked" in err_str
            or "harmful content" in err_str
        ):
            print(
                f"\n[提示] 组 [{pair_name}] 初次请求触发安全策略拦截 ({err_str})，正在使用温情成长简化提示词重试..."
            )
            interaction = client.interactions.create(
                model=MODEL_ID,
                input=build_pair_interaction_input(
                    start_uri=start_uri,
                    end_uri=end_uri,
                    prompt=fallback_prompt,
                ),
                timeout=1200.0,
            )
        else:
            raise

    steps = _get_field(interaction, "steps", []) or []
    print(f"[{pair_name}] 收到响应，共包含 {len(steps)} 个 step。")

    input_uris = {start_uri, end_uri}
    saved_videos: List[str] = []
    video_idx = 0
    base_video_name = f"{pair_name}_transition{file_suffix}"

    for s_i, step in enumerate(steps):
        step_type = _get_field(step, "type")
        content = _get_field(step, "content")
        print(f"  - Step {s_i}: type={step_type}")

        if debug_save_all_gcs_videos and step_type != "model_output":
            for gcs_uri in _extract_gcs_uris(step):
                if gcs_uri not in input_uris and (
                    gcs_uri.endswith(".mp4") or "video" in gcs_uri.lower()
                ):
                    debug_name = f"debug_{base_video_name}_step{s_i}_{Path(gcs_uri).name}"
                    transfer_gcs_movie_to_local(
                        gcs_uri, local_dir=str(out_path), local_filename=debug_name
                    )

        if step_type == "model_output" and content:
            for part in content:
                part_type = _get_field(part, "type")
                if part_type == "text":
                    text_val = _get_field(part, "text", "")
                    print(f"\n[{pair_name} Step {s_i} Text]:\n{text_val}")
                elif part_type == "video":
                    video_idx += 1
                    vid_b64 = _get_field(part, "data")
                    vid_uri = _get_field(part, "uri")
                    target_filename = (
                        f"{base_video_name}.mp4"
                        if video_idx == 1
                        else f"{base_video_name}_{video_idx}.mp4"
                    )
                    vid_file = out_path / target_filename

                    if vid_uri:
                        print(f"[{pair_name} Step {s_i} Video GCS URI]: {vid_uri}")
                        transfer_gcs_movie_to_local(
                            vid_uri,
                            local_dir=str(out_path),
                            local_filename=vid_file.name,
                        )
                        saved_videos.append(str(vid_file))
                    elif vid_b64:
                        vid_bytes = base64.b64decode(vid_b64)
                        vid_file.write_bytes(vid_bytes)
                        saved_videos.append(str(vid_file))
                        print(
                            f"[{pair_name}] 已保存视频 ({len(vid_bytes) / (1024*1024):.2f} MB): {vid_file}"
                        )

    if saved_videos:
        final_pair_video = out_path / f"{base_video_name}.mp4"
        if saved_videos[-1] != str(final_pair_video):
            import shutil
            shutil.copyfile(saved_videos[-1], final_pair_video)
        print(f"[{pair_name}] 最终过渡视频已保存至: {final_pair_video}")
        return str(final_pair_video)

    print(f"[警告] [{pair_name}] 未在响应中找到视频输出。")
    return None


def generate_three_pairs_videos(
    image_pairs: Optional[List[Dict[str, str]]] = None,
    prompt: str = UNIVERSAL_PAIR_TRANSITION_PROMPT,
    fallback_prompt: str = SAFE_GROWTH_FALLBACK_PROMPT,
    output_dir: str = ".",
    file_suffix: str = "",
) -> List[str]:
    """依次为 3 组首尾帧图片调用 Omni 生成 3 个平滑过渡视频。"""
    if image_pairs is None:
        image_pairs = DEFAULT_IMAGE_PAIRS

    client = genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=LOCATION,
        http_options=types.HttpOptions(
            headers={"Api-Revision": API_REVISION},
            timeout=1200000,
        ),
    )

    generated_files: List[str] = []
    for idx, pair in enumerate(image_pairs, start=1):
        pair_name = pair.get("name", f"pair_{idx}")
        start_uri = pair["start_uri"]
        end_uri = pair["end_uri"]
        try:
            video_path = generate_single_pair_video(
                client=client,
                pair_name=pair_name,
                start_uri=start_uri,
                end_uri=end_uri,
                prompt=prompt,
                fallback_prompt=fallback_prompt,
                output_dir=output_dir,
                file_suffix=file_suffix,
            )
            if video_path:
                generated_files.append(video_path)
        except Exception as e:
            print(f"\n[错误] 组 [{pair_name}] 生成失败: {e}")

    print("\n============================================================")
    print(f"全部任务完成，共成功生成 {len(generated_files)}/{len(image_pairs)} 个视频:")
    for vf in generated_files:
        print(f" - {vf}")
    return generated_files


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="输入3组首尾帧图片，调用 Gemini Omni 生成3个平滑动作与运镜过渡视频。"
    )
    parser.add_argument(
        "--pair-name",
        type=str,
        default=None,
        help="仅运行指定名称的图片组（例如 男生成长1 或 男生成长2）",
    )
    parser.add_argument(
        "--no-face-away",
        action="store_true",
        help="使用去掉“人物面部短暂不直接正对镜头”限制的对比版提示词，并保存为 *_no_face_away.mp4",
    )
    parser.add_argument(
        "--download-gcs",
        type=str,
        default=None,
        help="仅将指定的 gs:// 视频 URI 下载到本地目录用于 Debug",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=os.path.dirname(os.path.abspath(__file__)),
        help="本地视频保存目录",
    )
    args = parser.parse_args()

    if args.download_gcs:
        transfer_gcs_movie_to_local(args.download_gcs, local_dir=args.output_dir)
    else:
        pairs_to_run = DEFAULT_IMAGE_PAIRS
        if args.pair_name:
            pairs_to_run = [
                p for p in DEFAULT_IMAGE_PAIRS if p["name"] == args.pair_name
            ]
        selected_prompt = (
            UNIVERSAL_PAIR_TRANSITION_PROMPT_NO_FACE_AWAY
            if args.no_face_away
            else UNIVERSAL_PAIR_TRANSITION_PROMPT
        )
        selected_fallback = (
            SAFE_GROWTH_FALLBACK_PROMPT_NO_FACE_AWAY
            if args.no_face_away
            else SAFE_GROWTH_FALLBACK_PROMPT
        )
        suffix = "_no_face_away" if args.no_face_away else ""
        generate_three_pairs_videos(
            image_pairs=pairs_to_run,
            prompt=selected_prompt,
            fallback_prompt=selected_fallback,
            output_dir=args.output_dir,
            file_suffix=suffix,
        )

