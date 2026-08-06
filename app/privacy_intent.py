import re


TIME_RE=re.compile(
    r"(?<!\d)(?:(?P<minute>\d{1,3}):(?P<colon_second>\d{2}(?:\.\d+)?)|"
    r"(?P<zh_minute>\d+)\s*分\s*(?P<zh_second>\d+(?:\.\d+)?)\s*(?:秒)?|"
    r"(?P<second>\d+(?:\.\d+)?)\s*秒)"
)


def _time_tokens(line):
    values=[]
    for match in TIME_RE.finditer(line):
        if match.group("minute") is not None:
            seconds=int(match.group("minute"))*60+float(match.group("colon_second"))
        elif match.group("zh_minute") is not None:
            seconds=int(match.group("zh_minute"))*60+float(match.group("zh_second"))
        else:
            seconds=float(match.group("second"))
        values.append((match.start(),round(seconds,3)))
    return [value for _,value in sorted(values)]


def _region(line):
    regions=(
        (("左上","左上角"),(0.0,0.56,0.0,0.56)),
        (("右上","右上角"),(0.44,1.0,0.0,0.56)),
        (("左下","左下角"),(0.0,0.56,0.44,1.0)),
        (("右下","右下角"),(0.44,1.0,0.44,1.0)),
        (("左侧","左边","画面左"),(0.0,0.56,0.0,1.0)),
        (("右侧","右边","画面右"),(0.44,1.0,0.0,1.0)),
        (("中间","中央","中心","中部"),(0.22,0.78,0.0,1.0)),
    )
    for signals,bounds in regions:
        if any(signal in line for signal in signals):return bounds
    return 0.0,1.0,0.0,1.0


def _frames(line,signals,default):
    for signal in signals:
        match=re.search(rf"{signal}\s*(\d+)\s*帧",line)
        if match:return max(0,int(match.group(1)))
    return default


def parse_privacy_intent(revisions,fps=29.97):
    bodies=[str(value.get("body") or "") for value in revisions if isinstance(value,dict)]
    text="\n".join(bodies).strip();force_cover=[];suppress=[];warnings=[]
    for raw_line in text.splitlines():
        line=raw_line.strip().strip("*-• ")
        if not line or not re.search(r"马赛克|打码|遮挡",line):continue
        times=_time_tokens(line)
        if not times:
            warnings.append(f"未找到成片时间，已忽略：{line}")
            continue
        start=times[0]
        if len(times)>1:
            end=times[1]
            if start>=60 and 0<=end<60:
                same_minute=int(start//60)*60+end
                if same_minute>=start:end=same_minute
        elif re.search(r"之前|以前|(?<!提)前(?:的|都|全部|$)",line):
            start,end=0.0,start
        elif re.search(r"之后|以后|到结束|直至结束",line):
            end=86400.0
        else:end=start+1.0
        if end<start:start,end=end,start
        if end-start<1/max(float(fps),1):end=start+1/max(float(fps),1)
        x_min,x_max,y_min,y_max=_region(line)
        rule={
            "start":round(start,3),"end":round(end,3),
            "x_min":x_min,"x_max":x_max,"y_min":y_min,"y_max":y_max,
            "lead_frames":_frames(line,("提前",),2),
            "tail_frames":_frames(line,("延后","延长","尾随"),2),
            "max_gap_frames":_frames(line,("允许断开","跟踪间隔"),6),
            "label":line,
        }
        remove=bool(re.search(r"(?:去掉|删除|取消|不加|不要|无需).{0,8}(?:马赛克|打码|遮挡)|(?:马赛克|打码|遮挡).{0,8}(?:去掉|删除|取消|不加|不要|无需)",line))
        (suppress if remove else force_cover).append(rule)
    summary=[]
    if force_cover:summary.append(f"解析到 {len(force_cover)} 段人工普通马赛克；只处理指定成片时间和画面区域")
    if suppress:summary.append(f"解析到 {len(suppress)} 段明确不打码区域")
    if not force_cover and not suppress:summary.append("未解析到有效马赛克时间段；不会自动添加马赛克")
    summary.append("未明确提出马赛克时保持原画，不运行自动人脸遮挡")
    return {
        "schema":1,"kind":"privacy","text":text,
        "privacy_rules":{"force_cover":force_cover,"suppress":suppress,"force_owner":[]},
        "force_cover":force_cover,"suppress":suppress,
        "warnings":warnings,"summary":summary,
    }
