import os
import subprocess
import ctypes.util
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from .config import FACE_MODELS,OWNER_IDENTITY,PRIVACY_AVATAR

DETECTOR_MODEL=FACE_MODELS/"face_detection_yunet_2023mar.onnx"
RECOGNIZER_MODEL=FACE_MODELS/"face_recognition_sface_2021dec.onnx"
OWNER_THRESHOLD=float(os.getenv("PRIVACY_OWNER_COSINE","0.46"))
OWNER_CONTINUITY_FLOOR=float(os.getenv("PRIVACY_OWNER_CONTINUITY_FLOOR","0.38"))
DETECTION_SCORE=float(os.getenv("PRIVACY_DETECTION_SCORE","0.55"))
DETECTION_MAX_SIDE=int(os.getenv("PRIVACY_DETECTION_MAX_SIDE","960"))
TRACK_CONFIRM_FRAMES=int(os.getenv("PRIVACY_TRACK_CONFIRM_FRAMES","4"))
TRACK_LEAD_FRAMES=int(os.getenv("PRIVACY_TRACK_LEAD_FRAMES","5"))
TRACK_TAIL_FRAMES=int(os.getenv("PRIVACY_TRACK_TAIL_FRAMES","5"))
STATIC_FACE_MOTION=float(os.getenv("PRIVACY_STATIC_FACE_MOTION","1.8"))
AUTOMATIC_FACE_SCORE=float(os.getenv("PRIVACY_AUTOMATIC_FACE_SCORE","0.70"))
MIN_PRIMARY_FACE_HEIGHT_RATIO=float(os.getenv("PRIVACY_MIN_PRIMARY_FACE_HEIGHT_RATIO","0.055"))
MIN_PRIMARY_FACE_WIDTH_RATIO=float(os.getenv("PRIVACY_MIN_PRIMARY_FACE_WIDTH_RATIO","0.025"))
OWNER_REFERENCE_PATTERNS=("reference-*","profile-*","scene-left-*")
OWNER_REFERENCE_EXTENSIONS={".jpg",".jpeg",".png",".webp"}

def privacy_enabled(settings):
    privacy=(settings or {}).get("privacy") if isinstance(settings,dict) else None
    if isinstance(privacy,dict) and "cover_non_owner_faces" in privacy:
        return bool(privacy["cover_non_owner_faces"])
    if isinstance(privacy,dict) and "blur_non_owner_faces" in privacy:
        return bool(privacy["blur_non_owner_faces"])
    return True

def _identity_reference_files():
    return sorted({path for pattern in OWNER_REFERENCE_PATTERNS for path in OWNER_IDENTITY.glob(pattern) if path.suffix.lower() in OWNER_REFERENCE_EXTENSIONS})

def _required_files():
    references=_identity_reference_files()
    missing=[str(path) for path in (DETECTOR_MODEL,RECOGNIZER_MODEL) if not path.is_file()]
    if not references:missing.append(str(OWNER_IDENTITY/"reference-*.png"))
    if missing:raise RuntimeError("隐私处理缺少必要文件："+"；".join(missing))
    return references

class OwnerFaceMatcher:
    def __init__(self):
        references=_required_files()
        self.reference_names=[path.name for path in references]
        self.detector=cv2.FaceDetectorYN.create(str(DETECTOR_MODEL),"",(320,320),DETECTION_SCORE,0.3,5000)
        self.recognizer=cv2.FaceRecognizerSF.create(str(RECOGNIZER_MODEL),"")
        self.owner_features=[]
        for path in references:
            image=cv2.imread(str(path))
            if image is None:raise RuntimeError(f"无法读取本人参考照片：{path}")
            faces=self.detect(image)
            if not faces:raise RuntimeError(f"本人参考照片未检测到人脸：{path.name}")
            if path.name.startswith("scene-left-"):
                left=[value for value in faces if float(value[0])+float(value[2])/2<=image.shape[1]*0.68]
                if not left:raise RuntimeError(f"左侧本人参考画面没有检测到左侧人脸：{path.name}")
                face=max(left,key=lambda value:float(value[2])*float(value[3]))
            else:face=max(faces,key=lambda value:float(value[2])*float(value[3]))
            self.owner_features.append(self.feature(image,face))
        if not self.owner_features:raise RuntimeError("没有可用的本人面部特征")

    def detect(self,image):
        height,width=image.shape[:2]
        faces=[]
        for target_side in (DETECTION_MAX_SIDE,320):
            scale=min(1.0,target_side/max(width,height))
            if scale<1.0:resized=cv2.resize(image,(max(1,int(width*scale)),max(1,int(height*scale))),interpolation=cv2.INTER_AREA)
            else:resized=image
            self.detector.setInputSize((resized.shape[1],resized.shape[0]))
            detected=self.detector.detect(resized)[1]
            for value in detected if detected is not None else []:
                face=value.copy()
                if scale!=1.0:face[:14]/=scale
                if not any(self._iou(face,prior)>0.45 for prior in faces):faces.append(face)
        return faces

    @staticmethod
    def _iou(first,second):
        ax1,ay1,aw,ah=[float(value) for value in first[:4]];bx1,by1,bw,bh=[float(value) for value in second[:4]]
        ax2,ay2=ax1+aw,ay1+ah;bx2,by2=bx1+bw,by1+bh
        intersection=max(0.0,min(ax2,bx2)-max(ax1,bx1))*max(0.0,min(ay2,by2)-max(ay1,by1))
        return intersection/max(aw*ah+bw*bh-intersection,1.0)

    def feature(self,image,face):
        aligned=self.recognizer.alignCrop(image,np.asarray(face,dtype=np.float32))
        return self.recognizer.feature(aligned).copy()

    def owner_score(self,image,face):
        try:feature=self.feature(image,face)
        except cv2.error:return -1.0
        return max(float(self.recognizer.match(reference,feature,cv2.FaceRecognizerSF_FR_COSINE)) for reference in self.owner_features)

def _face_patch(image,face):
    height,width=image.shape[:2]
    x,y,w,h=[float(value) for value in face[:4]]
    x1=max(0,int(x));x2=min(width,int(x+w));y1=max(0,int(y));y2=min(height,int(y+h))
    if x2<=x1 or y2<=y1:return None
    region=image[y1:y2,x1:x2]
    return cv2.resize(cv2.cvtColor(region,cv2.COLOR_BGR2GRAY),(48,48),interpolation=cv2.INTER_AREA)

def _overlay_avatar(image,face,avatar):
    height,width=image.shape[:2];x,y,w,h=[float(value) for value in face[:4]];size=max(24,int(max(w*1.55,h*1.40)));cx=int(x+w/2);cy=int(y+h/2);desired_x1=cx-size//2;desired_y1=cy-size//2;desired_x2=desired_x1+size;desired_y2=desired_y1+size;x1=max(0,desired_x1);y1=max(0,desired_y1);x2=min(width,desired_x2);y2=min(height,desired_y2)
    if x2<=x1 or y2<=y1:return
    sticker=cv2.resize(avatar,(size,size),interpolation=cv2.INTER_AREA);sx=x1-desired_x1;sy=y1-desired_y1;sticker=sticker[sy:sy+(y2-y1),sx:sx+(x2-x1)]
    mask=np.zeros((size,size),dtype=np.uint8);cv2.circle(mask,(size//2,size//2),size//2-1,255,-1,lineType=cv2.LINE_AA);mask=mask[sy:sy+(y2-y1),sx:sx+(x2-x1)];alpha=(mask.astype(np.float32)/255.0)[...,None]
    image[y1:y2,x1:x2]=(sticker.astype(np.float32)*alpha+image[y1:y2,x1:x2].astype(np.float32)*(1-alpha)).astype(np.uint8)

def _tracking_score(face,prior):
    iou=OwnerFaceMatcher._iou(face,prior);ax,ay,aw,ah=[float(value) for value in face[:4]];bx,by,bw,bh=[float(value) for value in prior[:4]];distance=((ax+aw/2-bx-bw/2)**2+(ay+ah/2-by-bh/2)**2)**0.5/max(aw,ah,bw,bh,1.0)
    return max(iou,1.0-distance/1.2) if iou>=0.03 or distance<=0.85 else -1.0

def _track_box(track,frame_index):
    boxes=track["boxes"]
    if frame_index in boxes:return boxes[frame_index]
    prior=[index for index in boxes if index<=frame_index]
    if prior:return boxes[max(prior)]
    return boxes[min(boxes)]

def _track_is_live(track):
    return not track["owner"] and (track.get("manual_cover") or (track["seen"]>=TRACK_CONFIRM_FRAMES and track["max_motion"]>=STATIC_FACE_MOTION))

def _track_covers_frame(track,frame_index):
    lead=int(track.get("lead_frames",TRACK_LEAD_FRAMES));tail=int(track.get("tail_frames",TRACK_TAIL_FRAMES))
    return track["confirmed"] and not track["owner"] and track["start"]-lead<=frame_index<=track["last"]+tail

def _plausible_face(face,min_score=AUTOMATIC_FACE_SCORE):
    """Reject common YuNet false positives on hands, arms, cups and textured objects."""
    if face is None or len(face)<15:return False
    x,y,w,h=[float(value) for value in face[:4]]
    if w<=4 or h<=4 or float(face[14])<min_score or not 0.48<=w/h<=1.85:return False
    points=np.asarray(face[4:14],dtype=np.float32).reshape(5,2)
    normalized=(points-np.array([x,y],dtype=np.float32))/np.array([w,h],dtype=np.float32)
    if np.any(normalized<np.array([-0.18,-0.18])) or np.any(normalized>np.array([1.18,1.18])):return False
    eyes=normalized[:2];nose=normalized[2];mouth=normalized[3:]
    eye_distance=abs(float(eyes[0,0]-eyes[1,0]));mouth_distance=abs(float(mouth[0,0]-mouth[1,0]))
    eye_y=float(eyes[:,1].mean());mouth_y=float(mouth[:,1].mean())
    if not 0.10<=eye_distance<=0.92 or not 0.05<=mouth_distance<=0.95:return False
    if abs(float(eyes[0,1]-eyes[1,1]))>0.34 or abs(float(mouth[0,1]-mouth[1,1]))>0.38:return False
    if nose[1]<eye_y-0.08 or mouth_y<nose[1]-0.10 or mouth_y<eye_y+0.08:return False
    return True

def _primary_face(face,width,height):
    """Privacy is for featured people, not tiny distant pedestrians in the background."""
    if face is None or len(face)<4:return False
    return float(face[2])/max(width,1)>=MIN_PRIMARY_FACE_WIDTH_RATIO and float(face[3])/max(height,1)>=MIN_PRIMARY_FACE_HEIGHT_RATIO

def _rule_matches(rule,seconds,face,width,height):
    try:start=float(rule.get("start",0));end=float(rule.get("end",start))
    except (TypeError,ValueError):return False
    if seconds<start or seconds>end:return False
    if face is None:return True
    x,y,w,h=[float(value) for value in face[:4]];cx=(x+w/2)/max(width,1);cy=(y+h/2)/max(height,1)
    try:return float(rule.get("x_min",0))<=cx<=float(rule.get("x_max",1)) and float(rule.get("y_min",0))<=cy<=float(rule.get("y_max",1))
    except (TypeError,ValueError):return False

def _matching_rule(rules,key,seconds,face,width,height):
    for rule in (rules or {}).get(key,[]) if isinstance(rules,dict) else []:
        if isinstance(rule,dict) and _rule_matches(rule,seconds,face,width,height):return rule
    return None

def _manual_buffer_frames(rules):
    values=[TRACK_CONFIRM_FRAMES+TRACK_LEAD_FRAMES+2,TRACK_LEAD_FRAMES+TRACK_TAIL_FRAMES+2]
    for rule in (rules or {}).get("force_cover",[]) if isinstance(rules,dict) else []:
        if not isinstance(rule,dict):continue
        for key in ("lead_frames","tail_frames","max_gap_frames"):
            try:values.append(max(0,int(rule.get(key,0)))+2)
            except (TypeError,ValueError):pass
    return max(values)

def _encoder_args():
    requested=os.getenv("PRIVACY_VIDEO_ENCODER","auto").lower()
    has_nvenc=bool(ctypes.util.find_library("nvidia-encode"))
    if requested=="nvenc" or (requested=="auto" and has_nvenc):return ["-c:v","h264_nvenc","-preset","p4","-cq","23","-b:v","0"]
    return ["-c:v","libx264","-preset","fast","-crf","23"]

def inspect_image(path,matcher=None):
    matcher=matcher or OwnerFaceMatcher();image=cv2.imread(str(path))
    if image is None:raise RuntimeError(f"无法读取测试图片：{path}")
    results=[]
    for face in matcher.detect(image):
        score=matcher.owner_score(image,face)
        plausible=_plausible_face(face);primary=_primary_face(face,image.shape[1],image.shape[0])
        results.append({"box":[round(float(value),1) for value in face[:4]],"score":round(score,4),"detection_score":round(float(face[14]),4),"plausible":plausible,"primary":primary,"owner":plausible and score>=OWNER_THRESHOLD})
    return results

def anonymize_video(source,checkpoint=None,progress=None,manual_rules=None):
    source=Path(source);matcher=OwnerFaceMatcher();avatar=cv2.imread(str(PRIVACY_AVATAR));capture=cv2.VideoCapture(str(source))
    if avatar is None:raise RuntimeError(f"无法读取隐私遮挡头像：{PRIVACY_AVATAR}")
    if not capture.isOpened():raise RuntimeError(f"无法打开待打码视频：{source}")
    width=int(capture.get(cv2.CAP_PROP_FRAME_WIDTH));height=int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT));fps=float(capture.get(cv2.CAP_PROP_FPS) or 30);total=int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    temporary=source.with_name(source.stem+".privacy.tmp.mp4");log_path=source.with_name(source.stem+".privacy.ffmpeg.log")
    temporary.unlink(missing_ok=True);frames=0;written=0;owner_faces=0;covered_faces=0;suppressed_static_tracks=0;suppressed_invalid_faces=0;suppressed_distant_faces=0;suppressed_manual_faces=0;manual_covered_faces=0;manual_owner_faces=0;tracks={};next_track_id=1;buffer=deque();buffer_limit=_manual_buffer_frames(manual_rules)
    command=["ffmpeg","-y","-f","rawvideo","-pix_fmt","bgr24","-s",f"{width}x{height}","-r",f"{fps:.3f}","-i","pipe:0","-i",str(source),"-map","0:v:0","-map","1:a?",*_encoder_args(),"-pix_fmt","yuv420p","-r",f"{fps:.3f}","-c:a","copy","-shortest","-movflags","+faststart",str(temporary)]
    try:
        with log_path.open("wb") as log:
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=log)
            try:
                def write_buffered(force=False):
                    nonlocal written,suppressed_static_tracks
                    while buffer and (force or len(buffer)>buffer_limit):
                        frame_index,output=buffer.popleft()
                        for track in tracks.values():
                            if _track_covers_frame(track,frame_index):
                                box=_track_box(track,frame_index);seconds=frame_index/max(fps,1)
                                if not _matching_rule(manual_rules,"suppress",seconds,box,width,height):_overlay_avatar(output,box,avatar)
                        process.stdin.write(output.tobytes());written+=1
                        next_buffer_index=buffer[0][0] if buffer else frame_index+1
                        expired=[track_id for track_id,track in tracks.items() if next_buffer_index>track["last"]+TRACK_TAIL_FRAMES and frames-track["last"]>TRACK_TAIL_FRAMES]
                        for track_id in expired:
                            track=tracks.pop(track_id)
                            if not track["owner"] and not track["confirmed"] and track["seen"]>=TRACK_CONFIRM_FRAMES:suppressed_static_tracks+=1
                while True:
                    ok,frame=capture.read()
                    if not ok:break
                    frame_index=frames;seconds=frame_index/max(fps,1);detected=[]
                    for face in matcher.detect(frame):
                        force_rule=_matching_rule(manual_rules,"force_cover",seconds,face,width,height)
                        owner_rule=_matching_rule(manual_rules,"force_owner",seconds,face,width,height)
                        if _matching_rule(manual_rules,"suppress",seconds,face,width,height):suppressed_manual_faces+=1;continue
                        if not force_rule and not owner_rule and not _plausible_face(face):suppressed_invalid_faces+=1;continue
                        if not force_rule and not owner_rule and not _primary_face(face,width,height):suppressed_distant_faces+=1;continue
                        detected.append((face,force_rule,owner_rule))
                    available={track_id for track_id,track in tracks.items() if frame_index-track["last"]<=int(track.get("max_gap_frames",TRACK_TAIL_FRAMES+2))}
                    for face,force_rule,owner_rule in detected:
                        best_id=None;best_score=-1.0
                        for track_id in available:
                            value=_tracking_score(face,tracks[track_id]["last_box"])
                            if value>best_score:best_id=track_id;best_score=value
                        if best_id is None or best_score<0:
                            best_id=next_track_id;next_track_id+=1;tracks[best_id]={"start":frame_index,"last":frame_index,"last_box":face.copy(),"boxes":{},"seen":0,"owner":False,"owner_hits":0,"confirmed":False,"manual_cover":False,"lead_frames":TRACK_LEAD_FRAMES,"tail_frames":TRACK_TAIL_FRAMES,"max_gap_frames":TRACK_TAIL_FRAMES+2,"max_motion":0.0,"patch":None}
                        else:available.remove(best_id)
                        track=tracks[best_id];patch=_face_patch(frame,face)
                        if patch is not None and track["patch"] is not None:track["max_motion"]=max(track["max_motion"],float(cv2.absdiff(patch,track["patch"]).mean()))
                        if patch is not None:track["patch"]=patch
                        if force_rule:
                            track["manual_cover"]=True;track["owner"]=False;track["confirmed"]=True
                            for key,default in (("lead_frames",TRACK_LEAD_FRAMES),("tail_frames",TRACK_TAIL_FRAMES),("max_gap_frames",TRACK_TAIL_FRAMES+2)):
                                try:track[key]=max(int(track.get(key,default)),int(force_rule.get(key,default)))
                                except (TypeError,ValueError):pass
                            manual_covered_faces+=1
                        elif owner_rule:
                            track["owner"]=True;track["owner_hits"]=max(track["owner_hits"],2);track["confirmed"]=False;manual_owner_faces+=1
                        else:
                            score=matcher.owner_score(frame,face)
                            if score>=OWNER_THRESHOLD:track["owner_hits"]+=1
                            owner_match=score>=0.60 or track["owner_hits"]>=2 or (track["owner"] and score>=OWNER_CONTINUITY_FLOOR)
                            track["owner"]=track["owner"] or owner_match
                        track["seen"]+=1;track["last"]=frame_index;track["last_box"]=face.copy();track["boxes"][frame_index]=face.copy()
                        if track["owner"]:owner_faces+=1;track["confirmed"]=False
                        elif _track_is_live(track):track["confirmed"]=True;covered_faces+=1
                        oldest_kept=frame_index-buffer_limit-TRACK_TAIL_FRAMES-2
                        for old_index in [value for value in track["boxes"] if value<oldest_kept]:track["boxes"].pop(old_index,None)
                    buffer.append((frame_index,frame));frames+=1;write_buffered()
                    if frames%150==0:
                        if progress:progress(frames,total,owner_faces,covered_faces)
                        if checkpoint and not checkpoint(frames,total):raise InterruptedError("项目已暂停或停止")
                write_buffered(True)
                for track in tracks.values():
                    if not track["owner"] and not track["confirmed"] and track["seen"]>=TRACK_CONFIRM_FRAMES:suppressed_static_tracks+=1
                process.stdin.close();process.stdin=None
                return_code=process.wait()
                if return_code!=0:raise RuntimeError(f"人脸隐私视频编码失败，FFmpeg退出码 {return_code}")
            except Exception:
                if process.poll() is None:process.kill();process.wait()
                raise
        if not temporary.is_file() or temporary.stat().st_size<=0:raise RuntimeError("人脸隐私处理没有生成有效视频")
        temporary.replace(source);log_path.unlink(missing_ok=True)
        return {"frames":frames,"owner_faces":owner_faces,"avatar_covered_faces":covered_faces,"manual_covered_faces":manual_covered_faces,"manual_owner_faces":manual_owner_faces,"suppressed_invalid_faces":suppressed_invalid_faces,"suppressed_distant_faces":suppressed_distant_faces,"suppressed_manual_faces":suppressed_manual_faces,"suppressed_static_tracks":suppressed_static_tracks,"owner_reference_count":len(matcher.reference_names),"owner_reference_files":matcher.reference_names,"owner_threshold":OWNER_THRESHOLD,"automatic_face_score":AUTOMATIC_FACE_SCORE,"min_primary_face_height_ratio":MIN_PRIMARY_FACE_HEIGHT_RATIO,"min_primary_face_width_ratio":MIN_PRIMARY_FACE_WIDTH_RATIO,"lead_frames":TRACK_LEAD_FRAMES,"tail_frames":TRACK_TAIL_FRAMES,"static_motion_threshold":STATIC_FACE_MOTION}
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc,RuntimeError) and log_path.exists():
            detail=log_path.read_text(encoding="utf-8",errors="replace")[-2000:]
            raise RuntimeError(f"{exc}\n{detail}") from exc
        raise
    finally:capture.release()
