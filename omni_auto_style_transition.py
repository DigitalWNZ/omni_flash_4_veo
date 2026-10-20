import base64
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional
import google.auth
from google import genai
from google.genai import types
from google.cloud import storage

# 默认配置
PROJECT_ID = "gcr-evaluation-hub"
LOCATION = "global"
MODEL_ID = "gemini-omni-1.1-flash-preview"
API_REVISION = "2026-05-20"

DEFAULT_PERSON_URI = "gs://honor-omni-case/omni_flash_4_veo/国内真人7-1_9x16.png"
FALLBACK_PERSON_URI = "gs://honor-omni-case/omni_flash_4_veo/model_non_celebrity_9x16.png"

# 通用提示词：适用于仅输入1张人物参考图，由 Omni 自动搭配5种不同风格服装并生成5段自然过渡视频
UNIVERSAL_AUTO_STYLE_PROMPT = """你是一位顶级的时尚造型师与短视频导演。输入的图片是一张人物参考图（没有提供额外的服装图片）。
请根据图片中人物的外貌特征、性别、年龄感与个人气质，为其量身定制并搭配 5 套截然不同但高度契合其气质的服装风格，并生成一个包含 5 个连续片段的完整时尚换装视频（将 5 个片段合成为一个连贯的完整视频输出）。具体要求如下：

1. 人物与画幅一致性：全程严格保持输入图片中人物的面部五官、神态特征、身材比例及画面比例（与原图一致，如 9:16 竖屏）高度一致，光影质感真实自然。
2. 自主搭配 5 种不同风格造型：请根据人物特征自主设计 5 套风格鲜明、层次丰富的全身或半身穿搭（例如依次展现：① 都市摩登通勤风、② 休闲街头运动风、③ 优雅轻奢晚装/礼服风、④ 复古文艺/新中式风、⑤ 温柔慵懒度假/针织风；具体款式与配色请根据图中人物的性别与气质智能适配）。
3. 适配各套穿搭风格的动态表现：在第 1 至第 5 个片段中，人物依次穿上这 5 套不同风格的服装，并做出贴合当前穿搭风格的自然动作与神态互动（如通勤风自信整理衣领、运动风活力转身微笑、礼服风优雅侧身展示、文艺风轻抚袖口、休闲风自然插兜律动等）。
4. 片段间自然过渡与合成：请先生成人物分别试穿这 5 套风格造型的高清定妆参考图作为视觉锚点，再依次生成对应的 5 个动态视频片段，确保相邻片段之间的动作姿态、镜头运动和光影衔接流畅自然（例如利用连贯的转身、手部动作或平滑运镜完成换装过渡），避免生硬突兀的跳切，最终将 5 个片段合成为一个完整的视频输出。"""


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


def download_gcs_bytes(uri: str) -> bytes:
    """从 gs:// URI 下载二进制内容。"""
    if not uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI: {uri}")
    bucket_name, blob_name = uri[len("gs://") :].split("/", 1)
    client = get_storage_client()
    return client.bucket(bucket_name).blob(blob_name).download_as_bytes()


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


def build_interaction_input(
    person_uri: str = DEFAULT_PERSON_URI,
    prompt: str = UNIVERSAL_AUTO_STYLE_PROMPT,
    mime_type: str = "image/png",
) -> list:
    """组装仅含1张人物图 + 通用自主穿搭提示词的输入。"""
    content = [
        {
            "type": "image",
            "mime_type": mime_type,
            "uri": person_uri,
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


def concat_videos_ffmpeg(video_paths: List[str], output_path: str) -> bool:
    """当模型返回多个独立分镜视频但未自动拼接时，使用 ffmpeg 无缝合并。"""
    list_file = Path(output_path).parent / "concat_list_auto_style.txt"
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for vp in video_paths:
                f.write(f"file '{os.path.abspath(vp)}'\n")
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(output_path),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception as e:
        print(f"ffmpeg concat 提示: {e}")
        return False
    finally:
        if list_file.exists():
            list_file.unlink()


def generate_auto_style_transition_video(
    person_uri: str = DEFAULT_PERSON_URI,
    prompt: str = UNIVERSAL_AUTO_STYLE_PROMPT,
    output_dir: str = ".",
    auto_fallback_on_rai: bool = True,
    debug_save_all_gcs_videos: bool = True,
) -> List[str]:
    """调用 gemini-omni-1.1-flash-preview 根据单张人物图自动搭配5种风格服装并生成5段换装视频。"""
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    client = genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=LOCATION,
        http_options=types.HttpOptions(
            headers={"Api-Revision": API_REVISION},
            timeout=1200000,
        ),
    )

    print(f"正在调用 {MODEL_ID} (单张人像自主搭配 5 套不同风格服装) ...")
    print(f"- 人物参考图: {person_uri}")

    try:
        interaction = client.interactions.create(
            model=MODEL_ID,
            input=build_interaction_input(
                person_uri=person_uri,
                prompt=prompt,
            ),
            timeout=1200.0,
        )
    except Exception as e:
        err_msg = str(e)
        if auto_fallback_on_rai and "prominent individuals" in err_msg:
            print(
                f"\n[提示] 输入的人物图 ({person_uri}) 触发了 Google Responsible AI 公众人物/明星人脸保护策略:\n"
                f"  {err_msg}\n"
                f"正在自动切换为同目录下的非明星真人模特图进行验证: {FALLBACK_PERSON_URI}\n"
            )
            interaction = client.interactions.create(
                model=MODEL_ID,
                input=build_interaction_input(
                    person_uri=FALLBACK_PERSON_URI,
                    prompt=prompt,
                ),
                timeout=1200.0,
            )
        else:
            raise

    saved_videos: List[str] = []
    image_idx = 0
    video_idx = 0
    input_uris = {person_uri, FALLBACK_PERSON_URI}

    steps = _get_field(interaction, "steps", []) or []
    print(f"\n收到响应，共包含 {len(steps)} 个 step。")

    for s_i, step in enumerate(steps):
        step_type = _get_field(step, "type")
        content = _get_field(step, "content")
        print(f"- Step {s_i}: type={step_type}")

        # Debug: 如果非 model_output step 中包含 GCS 视频 URI，也自动下载到本地 debug 目录
        if debug_save_all_gcs_videos and step_type != "model_output":
            for gcs_uri in _extract_gcs_uris(step):
                if gcs_uri not in input_uris and (
                    gcs_uri.endswith(".mp4") or "video" in gcs_uri.lower()
                ):
                    debug_name = f"debug_auto_style_step{s_i}_{Path(gcs_uri).name}"
                    transfer_gcs_movie_to_local(
                        gcs_uri, local_dir=str(out_path), local_filename=debug_name
                    )

        if step_type == "model_output" and content:
            for part in content:
                part_type = _get_field(part, "type")
                if part_type == "text":
                    text_val = _get_field(part, "text", "")
                    print(f"\n[Step {s_i} Text]:\n{text_val}")
                elif part_type == "image":
                    image_idx += 1
                    img_b64 = _get_field(part, "data")
                    img_uri = _get_field(part, "uri")
                    img_bytes = None
                    if img_b64:
                        img_bytes = base64.b64decode(img_b64)
                    elif img_uri:
                        print(f"[Step {s_i} Image URI]: {img_uri}")
                        img_bytes = download_gcs_bytes(img_uri)
                    if img_bytes:
                        img_file = out_path / f"omni_auto_style_keyframe_{image_idx}.png"
                        img_file.write_bytes(img_bytes)
                        print(f"已保存造型定妆参考图 {image_idx}: {img_file}")
                elif part_type == "video":
                    video_idx += 1
                    vid_b64 = _get_field(part, "data")
                    vid_uri = _get_field(part, "uri")
                    vid_file = out_path / f"omni_auto_style_video_{video_idx}.mp4"
                    if vid_uri:
                        print(f"[Step {s_i} Video GCS URI]: {vid_uri}")
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
                            f"已保存内嵌 Base64 视频 {video_idx} ({len(vid_bytes) / (1024*1024):.2f} MB): {vid_file}"
                        )

    if saved_videos:
        final_video_path = out_path / "final_auto_style_transition.mp4"
        last_size = os.path.getsize(saved_videos[-1])
        first_size = os.path.getsize(saved_videos[0])
        if len(saved_videos) > 1 and last_size < first_size * 1.8:
            print("\n检测到返回了多个独立视频片段，正在合并为完整视频...")
            if not concat_videos_ffmpeg(saved_videos, str(final_video_path)):
                shutil.copyfile(saved_videos[-1], final_video_path)
        else:
            shutil.copyfile(saved_videos[-1], final_video_path)
        print(f"\n最终完整视频已保存至本地: {final_video_path}")

    return saved_videos


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="调用 Gemini Omni 根据单张人物图自动搭配5种不同风格服装并生成五段换装视频，或将 GCS 视频下载到本地进行 Debug。"
    )
    parser.add_argument(
        "--person-uri",
        type=str,
        default=DEFAULT_PERSON_URI,
        help="输入的人物参考图 GCS URI",
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
        generate_auto_style_transition_video(
            person_uri=args.person_uri,
            output_dir=args.output_dir,
        )
