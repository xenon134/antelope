import os

DEBUG_MODE = False

# Configuration
EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.mpeg', '.3gp', '.ts'}
OUTPUT_DIR = "ultrafast_output"

from collections import namedtuple
Job = namedtuple("Job", ["displayname", "get_command"])

def get_jobs(args):
    if len(args) > 0 and not args[0].startswith("-"):
        TARGET_DIR = args[0] 
    else:
        TARGET_DIR = input('Enter the target directory path: ').strip()
    # if OUTPUT_DIR is a simple name, put it inside TARGET_DIR. If it's a path, use it as is:
    OUTPUT_PATH = os.path.join(TARGET_DIR, OUTPUT_DIR) if '\\' not in OUTPUT_DIR and '/' not in OUTPUT_DIR else OUTPUT_DIR

    # ffmpeg -threads 1 -i %INPUT% -map 0 -vf scale=1366:768:flags=lanczos -c:v libx264 -crf 23 -preset ultrafast -c:a copy -c:s copy %OUT%
    get_command = lambda input_path: [
        "ffmpeg.bat",
        "-threads", "1",
        *(("-to", "10") if DEBUG_MODE else ()),  # only the first 10 seconds
        "-i", input_path, "-map", "0",
        "-vf", "scale=1366:768:flags=lanczos",
        "-c:v", "libx264", "-crf", "23", "-preset", "ultrafast",
        "-c:a", "copy", "-c:s", "copy",
        "-y", os.path.join(OUTPUT_PATH, os.path.basename(input_path))
    ]

    # # ffmpeg -threads 1 -i %INPUT% -map 0 -pix_fmt yuv420p -c:v h264_nvenc -rc vbr -cq 23 -preset p1 -c:a copy -c:s copy %OUTPUT%
    # get_command = lambda input_path: [
    #     "ffmpeg.bat",
    #     "-threads", "1",
    #     *(("-to", "10") if DEBUG_MODE else ()),  # only the first 10 seconds
    #     "-i", input_path, "-map", "0",
    #     "-vf", "scale=1366:768:flags=lanczos",
    #     "-c:v", "h264_nvenc", "-rc", "vbr", "-cq", "23",
    #     "-c:a", "copy", "-c:s", "copy",
    #     "-y", os.path.join(OUTPUT_PATH, os.path.basename(input_path))
    # ]

    os.makedirs(OUTPUT_PATH, exist_ok=True)
    try:
        filenames = os.listdir(TARGET_DIR)
    except FileNotFoundError:
        filenames = []
        print(f"Directory not found: {TARGET_DIR}")
        
    for filename in filenames:
        full_path = os.path.join(TARGET_DIR, filename)
        if os.path.isfile(full_path) and any(filename.lower().endswith(ext) for ext in EXTENSIONS):
            command = get_command(full_path)
            yield Job(get_command=lambda: command,
                displayname=os.path.basename(full_path))
