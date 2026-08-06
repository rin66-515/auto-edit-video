const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let currentProjectId=null;
let currentProjectStatus=null;
let assetCaptionSignature=null;
let allAssetCaptionsComplete=false;
const currentProjectStorageKey='vlog-current-project-id';

const statusNames={waiting_start:'等待启动',ready_for_audio:'等待音频清理',audio_cleaning:'音频清理中',audio_failed:'音频清理失败',ready_for_ai:'等待语音转写',transcribing:'语音转写中',asr_failed:'语音转写失败',ready_for_visual:'等待画面分析',visual_analyzing:'画面与字幕分析中',visual_failed:'视觉分析失败',draft_ready:'剪辑方案已生成',revision_requested:'等待修改方案',revision_planning:'正在修改方案',replan_requested:'等待版本重规划',superseded:'已由重规划版本替代',scheduled:'等待前序版本完成',render_requested:'等待母版渲染',rendering:'母版渲染中',render_failed:'母版渲染失败',caption_review_ready:'等待成片字幕校对',subtitle_render_requested:'等待字幕快速生成',subtitle_rendering:'字幕快速生成中',subtitle_render_failed:'字幕快速生成失败',review_ready:'等待最终审核',approved:'已批准锁定',published:'已发布',expired:'保留期已到，文件已删除',preprocessing:'素材导入中'};
const controlNames={importing:'正在导入',running:'运行中',pause_requested:'正在安全暂停',paused:'已暂停',stop_requested:'正在安全停止',stopped:'已停止'};
const actionNames={start:'启动项目',pause:'暂停',stop:'停止',continue:'继续'};
const workerNames={audio:'音频清理',asr:'语音识别',visual:'画面分析'};
const formatNames={long_16x9:'长篇 16:9',short_9x16:'短篇 9:16',both:'长篇 + 短篇'};
const revisionKindHelp={edit:'用于按成片时间删除、插入或调整局部内容；严格继承所选版本的其余部分。',shot:'用于替换明确镜头编号或指定原素材时间段。',audio:'只调整人声、BGM、降噪和环境音，不重新规划画面。',duration:'只调整局部时长或章节边界；请写明成片时间点。',style:'只调整局部节奏、转场或特效，不重新选整片素材。',privacy:'只按人工时间段添加或去除普通马赛克。',full_replan:'仅在需要推翻整条故事结构和重新选材时使用；明确成片时间码不能使用此项。'};
const escJson=value=>{try{return esc(typeof value==='string'?value:JSON.stringify(value,null,2))}catch{return esc(value)}};

async function api(url,opt){const r=await fetch(url,opt);if(!r.ok){let msg=await r.text();try{msg=JSON.parse(msg).detail||msg}catch{}throw new Error(msg)}return r.json()}

function displayState(p){
  const control=p.control?.desired_state||p.control_state||'stopped';
  return `${controlNames[control]||control} · ${statusNames[p.status]||p.status}`;
}

function projectButtons(ps){
  return ps.map(p=>`<button class="project ${Number(p.id)===Number(currentProjectId)?'active':''}" data-id="${p.id}"><b>${esc(p.title)}</b><span>${esc(displayState({...p,control:{desired_state:p.control_state}}))}</span></button>`).join('')||'<p class="muted">尚无项目</p>';
}

async function load(){
  const [h,ps]=await Promise.all([api('/api/health'),api('/api/projects')]);
  $('#health').textContent=`D盘可用 ${h.free_gib} GiB`;$('#health').className=h.free_gib<h.minimum_free_gib?'bad':'ok';
  $('#projects').innerHTML=projectButtons(ps);
  document.querySelectorAll('.project').forEach(b=>b.onclick=()=>show(b.dataset.id));
  if(!currentProjectId&&ps.length){
    const remembered=Number(localStorage.getItem(currentProjectStorageKey)),target=ps.find(value=>Number(value.id)===remembered)||ps[0];
    await show(target.id,true);
  }
}

function exportHtml(e,canDelete,pendingRevisionCount=0,pendingPrivacyCount=0,pendingFullReplanCount=0,isLatest=false){
  const files=e.path?(()=>{try{return JSON.parse(e.path)}catch{return[e.path]}})():[];
  const previews=files.map((_,i)=>`<video controls preload="metadata" src="/api/exports/${e.id}/files/${i}"></video>`).join('');
  let manifest={};try{manifest=JSON.parse(e.master_manifest||'{}')}catch{}
  const masters=Array.isArray(manifest.outputs)?manifest.outputs:[],masterPreviews=masters.map((output,i)=>`<div class="master-preview"><div><b>${esc(output.name||`成片${i+1}`)}</b><span>隐私母版 · 未指定马赛克时保持原画 · 软字幕预览</span></div><video controls preload="metadata" src="/api/exports/${e.id}/masters/${i}"><track kind="subtitles" srclang="zh" label="中日字幕" src="/api/exports/${e.id}/captions/${i}.vtt?r=${Number(e.caption_revision)||0}" default></video></div>`).join('');
  const activeStatuses=['rendering','subtitle_rendering'],deletable=canDelete&&!activeStatuses.includes(e.status),format=formatNames[e.format]||e.format,captionReady=Boolean(e.timeline_snapshot)&&(files.length||masters.length)&&['caption_review_ready','subtitle_render_requested','subtitle_rendering','subtitle_render_failed','review_ready','approved'].includes(e.status);
  const captionEditable=e.status==='caption_review_ready';
  const unlockButton=e.status==='approved'&&Number(e.locked)===1?`<button class="warning small" data-unlock-export="${e.id}" data-version="${esc(e.version)}" ${canDelete?'':'disabled'} title="${canDelete?'保留现有成片并恢复为待最终审核；之后可再次生成该格式的新版本':'请先暂停或停止项目'}">取消锁定</button>`:'';
  const captionTools=captionReady?`<div class="export-caption-tools"><span>成片字幕/时间点 · 已校对 ${Number(e.caption_revision)||0} 轮</span><button class="small" data-export-version-captions="${e.id}">导出 XLSX</button><button class="small" data-import-version-captions="${e.id}" data-version="${esc(e.version)}" ${captionEditable&&canDelete?'':'disabled'}>导入校对表</button>${captionEditable?`<button class="caption-lock" data-lock-captions="${e.id}" data-version="${esc(e.version)}" ${canDelete?'':'disabled'}>锁定字幕并生成成片</button>`:''}</div>`:'';
  const captionSummary=captionReady?`<div class="version-caption-summary" data-version-caption-summary="${e.id}"><p class="muted">正在读取该版本最终时间线字幕…</p></div>`:'';
  const discardMaster=captionEditable?`<button class="warning small" data-discard-master="${e.id}" data-version="${esc(e.version)}" ${canDelete?'':'disabled'} title="保留该版本的时间线与字幕修正，只删除当前母版文件并按当前方案重新渲染">废弃母版（按当前方案重渲染）</button>`:'';
  const privacyReady=captionEditable&&masters.length&&canDelete&&pendingPrivacyCount>0;
  const applyPrivacy=captionEditable?`<button class="small" data-apply-privacy="${e.id}" data-version="${esc(e.version)}" data-privacy-count="${pendingPrivacyCount}" ${privacyReady?'':'disabled'} title="${pendingPrivacyCount?`复用 ${e.version} 无字幕剪辑母版，只按 ${pendingPrivacyCount} 条人工意见生成普通马赛克快速版`:`请在下方选择 ${e.version} 并提交马赛克校对意见`}">应用马赛克意见（${pendingPrivacyCount}）</button>`:'';
  const revisionReady=captionEditable&&canDelete&&pendingRevisionCount>0;
  const applyRevisions=captionEditable?`<button class="${pendingFullReplanCount?'warning ':''}small" data-apply-revisions="${e.id}" data-version="${esc(e.version)}" data-revision-count="${pendingRevisionCount}" data-full-replan-count="${pendingFullReplanCount}" ${revisionReady?'':'disabled'} title="${pendingRevisionCount?pendingFullReplanCount?`明确执行完整重规划；${e.version} 来源母版仍会保留`:`继承 ${e.version} 的时间线，只应用 ${pendingRevisionCount} 条明确修改`:`请先在下方选择 ${e.version} 并提交版本意见`}">应用修改意见（${pendingRevisionCount}）</button>`:'';
  return `<li id="export-version-${e.id}" class="export-version${isLatest?' latest-version':''}"><div class="export-version-title"><span><b>${esc(e.version)}</b>${isLatest?'<em class="latest-version-badge">最新</em>':''} · ${esc(format)} · ${esc(statusNames[e.status]||e.status)}</span><span>${e.status==='review_ready'&&files.length?`<button data-approve="${e.id}">批准锁定</button>`:''}${unlockButton}${applyPrivacy}${discardMaster}${applyRevisions}<button class="danger small" data-delete-export="${e.id}" data-version="${esc(e.version)}" data-format="${esc(format)}" data-status="${esc(statusNames[e.status]||e.status)}" ${deletable?'':'disabled'} title="${deletable?'删除该版本的成片、字幕及快速修正母版':'请先暂停或停止项目；正在渲染的版本不能删除'}">删除历史版本</button></span></div>${manualReplanSummaryHtml(e)}${masterPreviews}${previews}${captionSummary}${captionTools}</li>`;
}

function revisionScopeLabel(revision,exports=[]){
  const source=revision.source_export_id?exports.find(value=>Number(value.id)===Number(revision.source_export_id)):null;
  if(!revision.source_version)return revision.status==='resolved'?'通用 · 已分别应用到长短篇方案':'通用 · 下一次分别规划长篇与短篇';
  const format=source?.format?` · ${formatNames[source.format]||source.format}`:'';
  const pendingAction=revision.kind==='privacy'?'马赛克校对':revision.kind==='full_replan'?'完整重规划':'增量修改';
  const applied=revision.applied_version?` · 已应用 ${revision.source_version} → ${revision.applied_version}`:revision.status==='open'?` · 待用于 ${revision.source_version} ${pendingAction}`:'';
  return `版本级 · ${revision.source_version}${format}${applied}`;
}

function revisionStatusLabel(revision){
  return {open:'待应用',resolved:'已应用到项目方案',applied:'已应用到新版本'}[revision.status]||revision.status;
}

function buttonGuideHtml(){
  return `<details class="button-guide"><summary>按钮说明与正确操作顺序</summary><div class="button-guide-grid"><div><b>项目控制</b><span>“单独渲染长篇/短篇”处理对应等待队列；“一起渲染”仅在两边都没有锁定、待处理意见或继续任务时使用。</span></div><div><b>局部修改</b><span>删除、插入、替换指定时间点时选择“局部剪辑调整”或“指定镜头替换”，再到来源版本点击“应用修改意见”。</span></div><div><b>快速修正</b><span>马赛克意见复用无字幕母版；字幕导入只更新字幕与时间点，不重渲染画面剪辑。</span></div><div><b>完整重规划</b><span>只在推翻整条故事结构和重新选材时使用，会重写时间线；系统会继承未明确改变的来源时长与音频策略。</span></div></div><p>推荐顺序：生成画面母版 → 审核镜头 → 应用局部/马赛克意见 → 锁定画面 → 导出并校对字幕 → 锁定字幕生成成片 → 最终批准。</p></details>`;
}

function revisionIntentHtml(intent){
  if(!intent||!Array.isArray(intent.summary))return '';
  const time=value=>{const seconds=Number(value);if(!Number.isFinite(seconds))return '自动选点';const minutes=Math.floor(seconds/60),rest=seconds-minutes*60;return minutes?`${minutes}:${String(Math.floor(rest)).padStart(2,'0')}`:`${rest.toFixed(rest%1?1:0)}秒`};
  const shotItems=(intent.recommendations||[]).map(value=>`<li><b>${value.priority==='required'?'必须':'推荐'}</b> · ${esc(value.asset_code||value.filename)} · ${esc(time(value.time_seconds))}${value.target_clip_index?` · 镜头${Number(value.target_clip_index)}`:''}${value.audio_mode==='dialogue'?' · 保留降噪人声/BGM自动压低':' · 静音'}</li>`).join('');
  const deletionItems=(intent.output_deletions||[]).map(value=>`<li><b>删除成片</b> · ${esc(time(value.start_seconds))}–${esc(time(value.end_seconds))} · ${(Number(value.end_seconds)-Number(value.start_seconds)).toFixed(1)}秒</li>`).join('');
  const insertionItems=(intent.insertions||[]).map(value=>`<li><b>插入镜头</b> · 成片 ${esc(time(value.output_at_seconds))} 后 · ${esc(value.asset_code||value.filename)} ${esc(time(value.source_start_seconds))}–${esc(time(value.source_end_seconds))}</li>`).join('');
  const privacyItems=(intent.force_cover||[]).map(value=>`<li><b>普通马赛克</b> · ${esc(time(value.start))}–${esc(time(value.end))} · 提前 ${Number(value.lead_frames)||0} 帧 · 延后 ${Number(value.tail_frames)||0} 帧</li>`).join('');
  const items=shotItems+deletionItems+insertionItems+privacyItems;
  const summary=intent.summary.map(value=>`<span>${esc(value)}</span>`).join('');
  const warnings=(intent.warnings||[]).map(value=>`<span class="warning-text">${esc(value)}</span>`).join('');
  return `<div class="revision-parsed"><b>系统解析</b>${summary}${items?`<ul>${items}</ul>`:''}${warnings}</div>`;
}

function manualReplanSummaryHtml(exportItem){
  let options={};try{options=JSON.parse(exportItem.render_options||'{}')}catch{}
  const manual=options.manual_replan;if(!manual||!['raw_reselection','structured_revision_v2'].includes(manual.mode))return '';
  const sources=(manual.sources||[]).map(source=>`${source.origin==='retained_existing'?'保留：':''}${esc(source.filename||source.asset_id)}${Number(source.start)>0?` · ${Number(source.start).toFixed(0)} 秒后`:''}`).join('、');
  const selection=manual.selection_policy==='recommendations_plus_ai_fill_preserve_count'?`保持 ${Number(manual.actual_clip_count)||0} 段，推荐镜头优先并由本地AI分析补齐`:manual.selection_policy==='required_sources_plus_curated_existing'?'已加入指定素材，并精简保留已有镜头':'仅使用上述素材';
  const repeatRule=manual.repeat_policy==='distinct_nonoverlapping_time_ranges'?'同一原片允许不同精彩片段，禁止重叠/相邻重复':manual.repeat_policy==='one_use_per_source'?'每个素材仅使用一次':'';
  const audioRule=manual.original_audio_mode==='selective_dialogue'?`仅镜头 ${(manual.voice_clips||[]).join('、')} 保留降噪人声，其余静音`:'无原声';
  return `<p class="manual-replan-summary"><b>本版已按人工选材重构</b> · ${sources}<br>${esc(selection)}${repeatRule?` · ${esc(repeatRule)}`:''} · BGM ${esc(manual.bgm_filename||options.bgm_filename||'未指定')} · ${esc(String(manual.target_seconds||'—'))} 秒 · ${esc(String(manual.flash_burst_count||0))} 组鼓点快闪 · ${esc(audioRule)}/无字幕母版</p>`;
}

function versionCaptionSummaryHtml(summary){
  const outputs=(summary.outputs||[]).map(output=>`${esc(output.name)} ${Number(output.captions)||0} 条`).join(' · ');
  const rows=(summary.rows||[]).map(row=>`<div class="review-summary-row"><div><b>${esc(row.output)}</b><time>${esc(row.start)}–${esc(row.end)}</time></div><span><i>原</i>${esc(row.source||'—')}</span><span><i>中</i>${esc(row.zh||'—')}</span><span><i>日</i>${esc(row.ja||'—')}</span><small>${esc(row.asset||'')}</small></div>`).join('');
  return `<div class="review-summary-line"><b>该版本最终成片字幕</b><span>${Number(summary.total)||0} 条 · <strong>${Number(summary.needs_review)||0} 条需人工复核</strong>${outputs?` · ${outputs}`:''}</span></div>${summary.needs_review?`<details><summary>展开 ${Number(summary.needs_review)} 条待复核字幕</summary><div class="review-summary-list">${rows}</div></details>`:'<p class="ok">该版本最终时间线没有标记为需人工复核的字幕。</p>'}`;
}

async function loadVersionCaptionSummaries(){
  await Promise.all([...document.querySelectorAll('[data-version-caption-summary]')].map(async container=>{
    try{container.innerHTML=versionCaptionSummaryHtml(await api(`/api/exports/${container.dataset.versionCaptionSummary}/caption-summary`))}
    catch(error){container.innerHTML=`<p class="muted">成片字幕汇总暂不可用：${esc(error.message)}</p>`}
  }));
}

function controlsHtml(p){
  const state=p.control?.desired_state||'stopped';
  const required=p.required_worker,requiredState=(p.workers||[]).find(x=>x.worker===required),workerReady=!required||Boolean(requiredState?.online);
  const renderStage=p.control?.resume_status==='render_requested';
  const queueCount=fmt=>{
    const queue=p.render_queue?.[fmt];
    if(queue)return Number(queue.ready??queue.pending??0);
    return (p.exports||[]).filter(e=>e.format===fmt&&['render_requested','render_failed'].includes(e.status)).length;
  };
  const longCount=queueCount('long_16x9'),shortCount=queueCount('short_9x16'),renderState=['stopped','paused'].includes(state),longVersion=p.render_queue?.long_16x9?.next_version,shortVersion=p.render_queue?.short_9x16?.next_version;
  const allowed={
    start:state==='stopped'&&Boolean(p.control?.resume_status)&&workerReady,
    pause:state==='running',
    stop:['running','pause_requested','paused'].includes(state),
    continue:state==='paused'&&workerReady
  };
  const continuableStatuses=new Set(['render_requested','rendering','render_failed','subtitle_render_requested','subtitle_rendering','subtitle_render_failed','scheduled']);
  const continuable=(p.exports||[]).filter(value=>continuableStatuses.has(value.status));
  const exportFormats=new Map((p.exports||[]).map(value=>[Number(value.id),value.format]));
  const pendingRevisionFormats=new Set((p.revisions||[]).filter(value=>value.status==='open'&&value.kind!=='privacy'&&value.source_export_id).map(value=>exportFormats.get(Number(value.source_export_id))).filter(Boolean));
  const longRevisionReason=pendingRevisionFormats.has('long_16x9')?'长篇存在待应用版本意见；请先在对应版本点击“应用修改意见”。':'';
  const shortRevisionReason=pendingRevisionFormats.has('short_9x16')?'短篇存在待应用版本意见；请先在对应版本点击“应用修改意见”。':'';
  const lockedShort=p.locked_short||(p.exports||[]).find(value=>value.format==='short_9x16'&&value.status==='approved'&&Number(value.locked)===1);
  const shortLockReason=lockedShort?`短篇 ${lockedShort.version} 已批准锁定；请先在历史版本中取消锁定。`:'';
  const combinedReasons=[];
  if(continuable.length)combinedReasons.push('已有待继续任务：'+continuable.map(value=>value.version+' · '+(formatNames[value.format]||value.format)).join('、')+'。请单独处理后再一起渲染。');
  if(shortLockReason)combinedReasons.push(shortLockReason);
  if(longRevisionReason)combinedReasons.push(longRevisionReason);
  if(shortRevisionReason)combinedReasons.push(shortRevisionReason);
  const combinedBlocked=combinedReasons.length>0,combinedReason=combinedReasons.join(' ');
  const renderButtonStatuses=new Set(['draft_ready','review_ready','caption_review_ready','approved','render_requested','rendering','render_failed']);
  if(renderStage||renderButtonStatuses.has(p.status)){
    const longLabel=longCount?`继续渲染 ${esc(longVersion||'')} · 长篇`:'单独渲染长篇';
    const shortLabel=shortCount?`继续渲染 ${esc(shortVersion||'')} · 短篇`:'单独渲染短篇';
    const renderButtons=`<button class="control-render-long" data-generate="long_16x9" ${renderState&&!longRevisionReason?'':'disabled'} title="${esc(longRevisionReason)}">${longLabel}</button><button class="control-render-short" data-generate="short_9x16" ${renderState&&!lockedShort&&!shortRevisionReason?'':'disabled'} title="${esc(shortLockReason||shortRevisionReason)}">${shortLabel}</button><button class="control-render-both" data-generate="both" ${renderState&&!combinedBlocked?'':'disabled'} title="${esc(combinedReason)}">长短篇一起渲染</button>`;
    return renderButtons+['pause','stop'].map(action=>`<button class="control-${action}" data-control="${action}" ${allowed[action]?'':'disabled'}>${actionNames[action]}</button>`).join('');
  }
  return Object.entries(actionNames).map(([action,label])=>`<button class="control-${action}" data-control="${action}" ${allowed[action]?'':'disabled'}>${label}</button>`).join('');
}

function workersHtml(p){
  return `<div class="workers">${(p.workers||[]).map(x=>`<span class="worker ${x.online?'online':'offline'}">${esc(workerNames[x.worker]||x.worker)} · ${x.online?'在线':'离线'}${p.required_worker===x.worker?' · 当前需要':''}</span>`).join('')}</div>${p.required_worker&&!p.workers?.find(x=>x.worker===p.required_worker)?.online?`<p class="worker-warning">当前阶段所需工作器离线，请运行 <code>scripts\\start.ps1</code>。</p>`:''}`;
}

function logsHtml(logs){
  if(!logs?.length)return '<p class="muted">暂无运行日志。启动、暂停、阶段切换和捕获到的错误会显示在这里。</p>';
  return logs.map(x=>`<div class="log-entry log-${esc(x.level)}"><time>${esc(new Date(x.created_at).toLocaleString())}</time><span class="log-level">${esc(x.level)}</span><b>${esc(x.stage)}</b><span>${esc(x.message)}</span>${x.details?`<details data-log-detail="${esc(x.id)}"><summary>查看详情</summary><pre>${escJson(x.details)}</pre></details>`:''}</div>`).join('');
}

function formatTime(seconds){
  const value=Math.max(0,Number(seconds)||0),minutes=Math.floor(value/60),rest=Math.floor(value%60);
  return `${String(minutes).padStart(2,'0')}:${String(rest).padStart(2,'0')}`;
}

function assetHtml(a){
  let x={};try{x=JSON.parse(a.analysis||'{}')}catch{}
  const captions=Array.isArray(x.bilingual_captions)?x.bilingual_captions:[],proofread=Number(x.caption_version)>=3;
  const captionList=captions.map(c=>`<div class="caption-row ${c.needs_review?'needs-review':''}"><time>${formatTime(c.start)}–${formatTime(c.end)}</time><span>${esc(c.zh||'—')}</span><span>${esc(c.ja||'—')}</span>${c.needs_review?'<b>需复核</b>':''}</div>`).join('');
  return `<div class="asset"><div><b>${esc(a.filename)}</b><span>${Math.round(a.duration||0)}秒 · ${a.width||'-'}×${a.height||'-'} · ${esc(a.codec||'-')}</span><p class="caption-version">${proofread?'二次精校字幕':'字幕未完成'} · ${captions.length} 句</p>${x.transcript?`<details data-asset-detail="${a.id}:transcript"><summary>查看原始识别稿</summary><p>${esc(x.transcript)}</p></details>`:''}${captions.length?`<details data-asset-detail="${a.id}:captions"><summary>查看全部二次精校中日字幕</summary><div class="caption-list">${captionList}</div></details>`:''}</div>${a.proxy_path?`<div class="proxy-preview"><video controls preload="none" data-proxy-asset="${a.id}" src="/api/assets/${a.id}/proxy"></video><div class="proxy-caption" data-caption-asset="${a.id}"><span class="muted">播放代理视频后，这里会同步显示二次精校字幕</span></div></div>`:''}</div>`;
}

function wireProxyCaptions(assets){
  for(const asset of assets){
    let analysis={};try{analysis=JSON.parse(asset.analysis||'{}')}catch{}
    const captions=Array.isArray(analysis.bilingual_captions)?analysis.bilingual_captions:[],video=document.querySelector(`[data-proxy-asset="${asset.id}"]`),output=document.querySelector(`[data-caption-asset="${asset.id}"]`);
    if(!video||!output)continue;
    const update=()=>{
      const current=Number(video.currentTime)||0,row=captions.find(c=>current>=Number(c.start)&&current<=Number(c.end));
      output.innerHTML=row?`<span class="caption-zh">${esc(row.zh||'')}</span><span class="caption-ja">${esc(row.ja||'')}</span>${row.needs_review?'<b class="caption-review">需人工复核</b>':''}`:`<span class="muted">${captions.length?'当前时间点没有对白字幕':'该素材没有识别到对白'}</span>`;
    };
    video.addEventListener('timeupdate',update);video.addEventListener('seeked',update);video.addEventListener('loadedmetadata',update);
  }
}

function captionsSignature(assets){
  return assets.map(asset=>{
    let analysis={};try{analysis=JSON.parse(asset.analysis||'{}')}catch{}
    return `${asset.id}:${analysis.caption_version||0}:${JSON.stringify(analysis.bilingual_captions||[])}`;
  }).join('|');
}

function captionsComplete(assets){
  return Boolean(assets.length)&&assets.every(asset=>{let analysis={};try{analysis=JSON.parse(asset.analysis||'{}')}catch{};return Number(analysis.caption_version)>=3});
}

function refreshAssetList(assets,force=false){
  const container=$('#assets-list'),signature=captionsSignature(assets);
  if(!container)return false;
  const changed=force||signature!==assetCaptionSignature;
  if(changed){
    const playback=new Map([...container.querySelectorAll('video[data-proxy-asset]')].map(video=>[video.dataset.proxyAsset,{time:video.currentTime,playing:!video.paused}]));
    const openDetails=new Set([...container.querySelectorAll('details[open][data-asset-detail]')].map(detail=>detail.dataset.assetDetail));
    container.innerHTML=assets.map(assetHtml).join('');assetCaptionSignature=signature;wireProxyCaptions(assets);
    container.querySelectorAll('details[data-asset-detail]').forEach(detail=>detail.open=openDetails.has(detail.dataset.assetDetail));
    for(const [assetId,state] of playback){
      const video=container.querySelector(`video[data-proxy-asset="${assetId}"]`);if(!video)continue;
      const restore=()=>{video.currentTime=state.time;if(state.playing)video.play().catch(()=>{})};
      if(video.readyState>=1)restore();else video.addEventListener('loadedmetadata',restore,{once:true});
    }
  }
  const meta=$('#asset-refresh-meta');
  if(meta)meta.textContent=`全素材完成后更新 · ${new Date().toLocaleTimeString()} 已同步${changed?'（发现更新）':''}`;
  return changed;
}

function currentSettingsPayload(p){
  const filename=String($('#short-bgm')?.value||'').trim();
  return {
    mode:$('#mode')?.value||p.mode,
    notes:$('#notes')?.value??p.notes,
    settings:{
      ...(p.settings||{}),
      short_bgm:{filename},
      editorial_rules:{
        common:String($('#common-rules')?.value||'').trim(),
        long_16x9:String($('#long-rules')?.value||'').trim(),
        short_9x16:String($('#short-rules')?.value||'').trim(),
      },
    },
  };
}

async function saveCurrentSettings(p){
  const payload=currentSettingsPayload(p);
  await api('/api/projects/'+p.id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  return payload;
}

function wireControls(p){
  document.querySelectorAll('[data-control]').forEach(b=>b.onclick=async()=>{
    const action=b.dataset.control;
    if(action==='stop'&&!confirm('停止会在当前素材处理完成后生效，已完成进度会保留。确定停止吗？'))return;
    document.querySelectorAll('[data-control]').forEach(x=>x.disabled=true);
    try{await api(`/api/projects/${p.id}/control/${action}`,{method:'POST'});await show(p.id)}catch(e){alert(e.message);await show(p.id)}
  });
  document.querySelectorAll('[data-generate]').forEach(b=>b.onclick=async()=>{
    const format=b.dataset.generate,label=formatNames[format]||format;
    const includesShort=format==='short_9x16'||format==='both';
    try{
      if(includesShort){
        const payload=await saveCurrentSettings(p),shortBgm=payload.settings.short_bgm.filename;
        if(!shortBgm&&!confirm('尚未指定短篇 BGM。确定以无 BGM 方式继续吗？\n\n选择“取消”后可在制作设定中填写音乐库文件名。')){$('#short-bgm')?.focus();return}
      }
      const message=format==='both'?`将各创建或继续一个长篇和短篇版本，并依次渲染。继续吗？`:`将只创建或继续一个${label}版本，其他格式不会启动。继续吗？`;
      if(!confirm(message))return;
      document.querySelectorAll('[data-control],[data-generate]').forEach(x=>x.disabled=true);
      await api(`/api/projects/${p.id}/generate/${format}`,{method:'POST'});await show(p.id);
    }catch(e){alert(e.message);await show(p.id)}
  });
}

async function show(id,revealLatest=false){
  currentProjectId=Number(id);
  localStorage.setItem(currentProjectStorageKey,String(currentProjectId));
  const p=await api('/api/projects/'+id),uploaded=new Set(p.uploads.filter(x=>x.completed_at).map(x=>x.platform)),story=p.settings?.story_plan,approved=new Set(p.exports.filter(x=>x.status==='approved'&&x.path&&x.path!=='[]').map(x=>x.format)),editorialRules=p.settings?.editorial_rules||{};
  const pendingRevisions=Object.fromEntries(p.exports.map(e=>[e.id,p.revisions.filter(r=>r.status==='open'&&r.kind!=='privacy'&&Number(r.source_export_id)===Number(e.id)).length]));
  const pendingFullReplans=Object.fromEntries(p.exports.map(e=>[e.id,p.revisions.filter(r=>r.status==='open'&&r.kind==='full_replan'&&Number(r.source_export_id)===Number(e.id)).length]));
  const pendingPrivacy=Object.fromEntries(p.exports.map(e=>[e.id,p.revisions.filter(r=>r.status==='open'&&r.kind==='privacy'&&Number(r.source_export_id)===Number(e.id)).length]));
  const replanTargets=p.exports.filter(e=>e.status==='caption_review_ready');
  const canManageRevisions=['stopped','paused'].includes(p.control?.desired_state);
  currentProjectStatus=p.status;
  const rawDeleteReady=!p.raw_deleted_at&&['stopped','paused'].includes(p.control?.desired_state)&&approved.has('long_16x9')&&approved.has('short_9x16');
  $('#detail').innerHTML=`
    <div class="title"><div><h2>${esc(p.title)}</h2><span id="project-status" class="tag">${esc(displayState(p))}</span></div><small>${p.assets.length} 个视频</small></div>
    <div id="project-error">${p.error?`<div class="error">${esc(p.error)}</div>`:''}</div>
    <div class="card control-card">
      <div><h3>项目控制</h3><div id="control-buttons" class="control-buttons">${controlsHtml(p)}</div><div id="worker-status">${workersHtml(p)}</div><p class="muted">剪辑方案完成后可选择单独渲染长篇、单独渲染短篇或长短篇一起渲染。母版生成后会自动停在字幕校对。</p></div>
      <div class="control-location"><span>当前阶段</span><b id="control-stage">${esc(p.control?.stage||statusNames[p.status]||p.status)}</b><span>当前位置</span><b id="control-item">${esc(p.control?.item||'等待操作')}</b></div>
    </div>
    <div class="cards">
      <div class="card rule-settings"><h3>制作设定与规则范围</h3><label>模式<select id="mode"><option value="existing">已有素材重建故事</option><option value="scripted">脚本驱动拍摄</option></select></label><label>素材与故事背景（只写事实）<textarea id="notes" placeholder="人物、地点、事件顺序、值得记录的内容……">${esc(p.notes)}</textarea></label><label>通用补充规则<textarea id="common-rules" placeholder="同时适用于长篇和短篇的规则">${esc(editorialRules.common||'')}</textarea></label><div class="rule-scope-grid"><label class="long-rule">长篇 16:9 独有规则<textarea id="long-rules" placeholder="长篇时长、叙事、音频、章节等">${esc(editorialRules.long_16x9||'')}</textarea></label><label class="short-rule">短篇 9:16 独有规则<textarea id="short-rules" placeholder="短篇节奏、BGM、选材、特效等">${esc(editorialRules.short_9x16||'')}</textarea></label></div><label>短篇指定 BGM<input id="short-bgm" value="${esc(p.settings?.short_bgm?.filename||'')}" placeholder="例如 my-song.mp3（放入音乐库）"></label><p class="muted">规则优先级：版本明确意见 ＞ 对应格式独有规则 ＞ 通用规则 ＞ AI判断。长篇与短篇分别规划并从全部素材独立选材；BGM只作用于短篇。</p><button id="save">保存设定</button></div>
      <div class="card"><h3>AI剪辑方案</h3>${story?`<p>${esc(story.summary||story.title||'')}</p><details><summary>查看完整可编辑时间线</summary><pre>${escJson(story)}</pre></details>`:'<p class="muted">等待音频、字幕和画面分析完成。</p>'}</div>
    </div>
    <div class="card export-card"><h3>历史版本与成片审核</h3><p class="muted export-help">版本修改默认继承来源版本的镜头、顺序、转场、时长、BGM、字幕和马赛克，只改变意见明确指出的内容。马赛克与字幕仍使用各自快速流程；来源版本不会因生成新版本而自动删除。</p><ul>${p.exports.map((e,index)=>exportHtml(e,['stopped','paused'].includes(p.control?.desired_state),pendingRevisions[e.id]||0,pendingPrivacy[e.id]||0,pendingFullReplans[e.id]||0,index===0)).join('')||'<li>尚无输出</li>'}</ul><input id="export-caption-workbook-file" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" hidden></div>
    <div class="card"><h3>审核修改意见</h3><p class="muted">版本意见只作用于所选长篇或短篇，绝不会改动另一格式。项目通用意见会在下一次明确启动项目级重规划时分别生成两套方案。版本级意见默认增量修改，只有“完整重规划”会重写该格式时间线。</p>${buttonGuideHtml()}<div class="row replan-controls"><label>关联范围<select id="revision-target"><option value="">项目通用（长短篇分别规划）</option>${replanTargets.map(e=>`<option value="${e.id}">基于 ${esc(e.version)} · ${esc(formatNames[e.format]||e.format)}</option>`).join('')}</select></label><label>意见类型<select id="kind"><option value="edit">局部剪辑调整</option><option value="shot">指定镜头替换</option><option value="audio">音频精调</option><option value="duration">局部时长/章节</option><option value="style">局部风格/节奏</option><option value="privacy">马赛克校对</option><option value="full_replan">完整重规划（慎用）</option></select></label><button id="open-replan-dialog">填写修改意见</button></div><p id="revision-kind-help" class="revision-kind-help"></p><ul>${p.revisions.map(r=>`<li class="revision-item"><div class="revision-item-copy"><div class="revision-meta"><span class="revision-scope">${esc(revisionScopeLabel(r,p.exports))}</span><span class="revision-state">${esc(revisionStatusLabel(r))}</span></div><div class="revision-body"><b>${esc(r.kind)}</b> ${esc(r.body)}</div>${revisionIntentHtml(r.parsed_intent)}</div><button class="danger small" data-delete-revision="${r.id}" data-revision-status="${esc(r.status)}" ${canManageRevisions?'':'disabled'} title="${canManageRevisions?'删除该条修改意见；已完成的意见不会回滚现有方案或版本':'请先暂停或停止项目'}">删除意见</button></li>`).join('')||'<li>暂无修改意见</li>'}</ul></div>
    <div class="card log-card"><div class="log-title"><h3>运行日志</h3><div class="log-actions"><span id="log-refresh-meta" class="muted">每 60 秒自动刷新 · 最近 200 条</span><button id="refresh-logs" class="small">立即刷新</button></div></div><div id="project-logs" class="project-logs">${logsHtml(p.logs)}</div></div>
    <div class="card raw-card"><h3>原片清理</h3>${p.raw_deleted_at?`<p class="ok">原片已于 ${esc(new Date(p.raw_deleted_at).toLocaleString())} 永久删除；代理、字幕和成片仍保留。</p>`:`<p class="muted">长篇和短篇均“批准锁定”并停止项目后，才能立即删除 inbox 中本项目的原始视频。删除后无法重新剪辑原片。</p><button id="delete-raw" class="danger" ${rawDeleteReady?'':'disabled'}>审核完成并立即删除原片</button>`}</div>
    <details class="source-section"><summary>素材与代理预览 · ${p.assets.length} 个（仅供追溯）</summary><div class="card"><div class="asset-title"><h3>源素材识别结果</h3><div class="log-actions"><span id="asset-refresh-meta" class="muted">全素材完成后更新 · 可手动刷新</span><button id="refresh-assets" class="small">刷新素材</button></div></div><p class="muted asset-help">代理字幕仅用于追溯原始识别结果，不再作为人工终审入口。最终人工字幕审核请在上方各母版版本的“最终成片字幕”中完成。</p><div id="assets-list" class="assets"></div></div></details>
    <div class="card"><h3>四平台上传确认</h3><p class="muted">四项全部确认后立即清理临时渲染缓存，并开始原片/代理/音频等中间文件的14天保留期；批准成片从“批准/四平台确认”较晚时间起保留90天。已确认的平台可再次点击取消；取消不会恢复已经删除的文件。</p>${['youtube','bilibili','douyin','xiaohongshu'].map(x=>`<button data-upload="${x}" data-confirmed="${uploaded.has(x)}" class="${uploaded.has(x)?'confirmed':''}">${x} · ${uploaded.has(x)?'✓ 取消确认':'确认已上传'}</button>`).join('')}</div>
    <dialog id="replan-dialog" class="confirm-dialog replan-dialog"><form method="dialog"><h3>填写修改意见</h3><p id="replan-dialog-meta" class="muted"></p><textarea id="revision" placeholder="音频示例：BGM保持到2秒后出现人声再压低。镜头示例：镜头02替换为某素材20秒后。马赛克示例：22.1秒到22.8秒右侧人物加马赛克。"></textarea><div id="revision-intent-preview" class="revision-preview muted">提交前可先查看系统解析结果；默认沿用关联版本。</div><div class="confirm-dialog-actions"><button value="cancel">取消</button><button id="preview-revision" type="button">解析预览</button><button id="send" type="button">提交修改意见</button></div></form></dialog><dialog id="delete-export-dialog" class="confirm-dialog"><form method="dialog"><div class="confirm-dialog-icon">!</div><h3>确认删除历史版本</h3><p id="delete-export-summary"></p><p class="warning-text">该版本的最终成片、字幕校对记录、无字幕剪辑母版和隐私母版都会永久删除，无法恢复。原片、代理素材和其他版本不受影响。</p><div class="confirm-dialog-actions"><button value="cancel">取消</button><button id="confirm-delete-export" type="button" class="danger">确认永久删除</button></div></form></dialog>`;
  $('#mode').value=p.mode;
  $('#save').onclick=async()=>{try{await saveCurrentSettings(p);await show(p.id)}catch(e){alert(e.message)}};
  const replanDialog=$('#replan-dialog');
  const updateRevisionKindHelp=()=>{$('#revision-kind-help').textContent=revisionKindHelp[$('#kind').value]||''};updateRevisionKindHelp();$('#kind').onchange=updateRevisionKindHelp;
  $('#open-replan-dialog').onclick=()=>{const target=$('#revision-target').selectedOptions[0]?.textContent||'项目通用（长短篇分别规划）',kind=$('#kind').selectedOptions[0]?.textContent||'剪辑结构';$('#replan-dialog-meta').textContent=`关联：${target}　·　类型：${kind}。${revisionKindHelp[$('#kind').value]||''}`;$('#revision-intent-preview').className='revision-preview muted';$('#revision-intent-preview').textContent='提交前可先查看系统解析结果。';replanDialog.showModal();setTimeout(()=>$('#revision').focus(),0)};
  const previewRevision=async()=>{if(!$('#revision').value.trim()){ $('#revision').focus();return }const sourceExportId=Number($('#revision-target').value)||null,result=await api(`/api/projects/${p.id}/revisions/preview`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:$('#kind').value,body:$('#revision').value.trim(),source_export_id:sourceExportId})});$('#revision-intent-preview').className='revision-preview';$('#revision-intent-preview').innerHTML=revisionIntentHtml(result)};
  $('#preview-revision').onclick=async()=>{try{await previewRevision()}catch(e){alert(e.message)}};
  $('#send').onclick=async()=>{if(!$('#revision').value.trim()){ $('#revision').focus();return }try{const sourceExportId=Number($('#revision-target').value)||null;const result=await api(`/api/projects/${p.id}/revisions`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:$('#kind').value,body:$('#revision').value.trim(),source_export_id:sourceExportId})});replanDialog.close();const next=result.waiting_for==='apply_privacy'?`随后点击 ${result.source_version} 的“应用马赛克意见”。`:result.source_version?`随后点击 ${result.source_version} 的“应用修改意见”。`:'点击启动项目后将更新项目级方案。';alert(`意见已记录并完成规则解析。${next}`);await show(p.id)}catch(e){alert(e.message)}};
  document.querySelectorAll('[data-delete-revision]').forEach(b=>b.onclick=async()=>{const completed=['applied','resolved'].includes(b.dataset.revisionStatus);const message=completed?'删除这条已完成的规划意见记录？\n\n不会回滚已经生成的剪辑方案或版本。':'删除这条待应用的规划意见？\n\n它将不再参与之后的重规划。';if(!confirm(message))return;try{await api(`/api/projects/${p.id}/revisions/${b.dataset.deleteRevision}`,{method:'DELETE'});await show(p.id)}catch(e){alert(e.message)}});
  document.querySelectorAll('[data-approve]').forEach(b=>b.onclick=async()=>{try{await api(`/api/exports/${b.dataset.approve}/approve`,{method:'POST'});await show(p.id)}catch(e){alert(e.message)}});
  document.querySelectorAll('[data-unlock-export]').forEach(b=>b.onclick=async()=>{
    if(!confirm(`取消 ${b.dataset.version} 的批准锁定？\n\n现有成片会保留并恢复为待最终审核；之后将重新允许生成该格式的新版本。`))return;
    try{await api(`/api/exports/${b.dataset.unlockExport}/unlock`,{method:'POST'});await show(p.id)}catch(e){alert(e.message)}
  });
  document.querySelectorAll('[data-export-version-captions]').forEach(b=>b.onclick=()=>{window.location.href=`/api/exports/${b.dataset.exportVersionCaptions}/captions.xlsx`});
  const exportCaptionInput=$('#export-caption-workbook-file');
  document.querySelectorAll('[data-import-version-captions]').forEach(b=>b.onclick=()=>{exportCaptionInput.dataset.exportId=b.dataset.importVersionCaptions;exportCaptionInput.dataset.version=b.dataset.version;exportCaptionInput.click()});
  if(exportCaptionInput)exportCaptionInput.onchange=async event=>{
    const input=event.currentTarget,file=input.files?.[0],version=input.dataset.version,exportId=input.dataset.exportId;if(!file)return;
    try{
      if(file.size>20*1024*1024)throw new Error('XLSX超过20 MiB，已拒绝导入');
      if(!confirm(`将把 ${file.name} 的时间点和字幕修正应用到 ${version}。本轮旧记录会自动备份，画面母版不会重渲染。确定继续吗？`))return;
      const dataUrl=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('读取XLSX失败'));reader.readAsDataURL(file)});
      const result=await api(`/api/exports/${exportId}/captions/import`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:file.name,xlsx_base64:String(dataUrl).split(',',2)[1]})});
      alert(result.changed_rows?(result.same_version?`已在 ${result.version} 完成第 ${result.caption_revision} 轮校对，共修正 ${result.changed_rows} 条字幕或时间点。可继续导出检查，确认后再锁定字幕。`:`已修正 ${result.changed_rows} 条字幕或时间点并创建 ${result.version}。`):result.message);await show(p.id);
    }catch(e){alert(e.message)}finally{input.value='';delete input.dataset.exportId;delete input.dataset.version}
  };
  document.querySelectorAll('[data-lock-captions]').forEach(b=>b.onclick=async()=>{
    if(!confirm(`确认锁定 ${b.dataset.version} 当前字幕，并从马赛克母版快速生成最终成片吗？`))return;
    try{await api(`/api/exports/${b.dataset.lockCaptions}/captions/lock`,{method:'POST'});await show(p.id)}catch(e){alert(e.message)}
  });
  document.querySelectorAll('[data-apply-privacy]').forEach(b=>b.onclick=async()=>{
    const count=Number(b.dataset.privacyCount)||0;
    if(!confirm(`将复用 ${b.dataset.version} 的无字幕剪辑母版，只按 ${count} 条人工意见生成普通马赛克快速版。\n\n剪辑、BGM、字幕和原版本不会改变。确定继续吗？`))return;
    try{const result=await api(`/api/exports/${b.dataset.applyPrivacy}/privacy-revisions/apply`,{method:'POST'});alert(`已创建 ${result.version} 马赛克快速版，当前保持停止。需要时点击顶部对应格式按钮开始渲染。`);await show(p.id)}catch(e){alert(e.message)}
  });

  document.querySelectorAll('[data-discard-master]').forEach(b=>b.onclick=async()=>{
    if(!confirm(`将删除 ${b.dataset.version} 当前无字幕剪辑母版和马赛克母版，并把同一版本重新加入渲染队列。\n\n时间线、字幕修正和版本号会保留；操作后需在顶部点击对应格式的渲染按钮。确定继续吗？`))return;
    try{const result=await api(`/api/exports/${b.dataset.discardMaster}/master/discard`,{method:'POST'});alert(`${b.dataset.version} 母版已废弃，释放约 ${(result.released_bytes/1073741824).toFixed(2)} GiB。请从顶部重新启动该格式渲染。`);await show(p.id)}catch(e){alert(e.message)}
  });
  document.querySelectorAll('[data-apply-revisions]').forEach(b=>b.onclick=async()=>{
    const count=Number(b.dataset.revisionCount)||0,fullCount=Number(b.dataset.fullReplanCount)||0;
    const message=fullCount?`你明确选择了“完整重规划”。系统将读取 ${count} 条意见并重写整条时间线。\n\n${b.dataset.version} 的原时间线和母版会保留用于回退；新方案需要再次启动渲染。确定执行完整重规划吗？`:`将严格继承 ${b.dataset.version} 的镜头、顺序、转场、总时长、BGM、字幕和马赛克，只应用 ${count} 条意见中明确指出的局部变化。\n\n来源版本会保留。确定创建增量新版本吗？`;
    if(!confirm(message))return;
    try{
      const result=await api(`/api/exports/${b.dataset.applyRevisions}/revisions/apply`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm_full_replan:fullCount>0})});
      alert(result.mode==='full_replan'?`${b.dataset.version} 已进入完整重规划等待状态，来源母版已保留。请点击“启动项目”生成新方案。`:`已基于 ${b.dataset.version} 创建 ${result.version}，只应用 ${result.operations.length} 项局部修改，当前保持停止。`);
      await show(p.id)
    }catch(e){alert(e.message)}
  });
  const deleteDialog=$('#delete-export-dialog'),deleteSummary=$('#delete-export-summary'),confirmDelete=$('#confirm-delete-export');
  document.querySelectorAll('[data-delete-export]').forEach(b=>b.onclick=()=>{deleteDialog.dataset.exportId=b.dataset.deleteExport;deleteDialog.dataset.version=b.dataset.version;deleteSummary.textContent=`即将删除 ${b.dataset.version} · ${b.dataset.format} · ${b.dataset.status}`;deleteDialog.showModal()});
  if(confirmDelete)confirmDelete.onclick=async()=>{const exportId=deleteDialog.dataset.exportId,version=deleteDialog.dataset.version;confirmDelete.disabled=true;confirmDelete.textContent='删除中…';try{const result=await api(`/api/exports/${exportId}`,{method:'DELETE'});deleteDialog.close();alert(`${version} 已删除，释放约 ${(result.released_bytes/1073741824).toFixed(2)} GiB`);await show(p.id)}catch(e){alert(e.message);confirmDelete.disabled=false;confirmDelete.textContent='确认永久删除'}};
  document.querySelectorAll('[data-upload]').forEach(b=>b.onclick=async()=>{try{await api(`/api/projects/${p.id}/uploads/${b.dataset.upload}`,{method:b.dataset.confirmed==='true'?'DELETE':'POST'});await show(p.id)}catch(e){alert(e.message)}});
  if($('#refresh-logs'))$('#refresh-logs').onclick=()=>refreshLive(true);
  if($('#refresh-assets'))$('#refresh-assets').onclick=()=>refreshLive(true,'assets');
  if($('#delete-raw'))$('#delete-raw').onclick=async()=>{if(!confirm(`将永久删除 ${p.source_dir} 内本项目的 ${p.assets.length} 个原始视频。代理、字幕和已批准成片保留。此操作无法撤销，确定继续吗？`))return;try{const result=await api(`/api/projects/${p.id}/raw/delete`,{method:'POST'});alert(`已删除 ${result.deleted_files} 个原片，释放约 ${(result.deleted_bytes/1073741824).toFixed(1)} GiB`);await show(p.id)}catch(e){alert(e.message)}};
  refreshAssetList(p.assets,true);allAssetCaptionsComplete=captionsComplete(p.assets);wireControls(p);await loadVersionCaptionSummaries();
  const latest=p.exports[0],waitingForReview=latest&&['caption_review_ready','review_ready','approved'].includes(latest.status);
  if(revealLatest&&waitingForReview)requestAnimationFrame(()=>document.querySelector(`#export-version-${latest.id}`)?.scrollIntoView({block:'start'}));
  await load();
}

async function refreshLive(manual=false,source='logs'){
  if(!currentProjectId)return;
  const refreshButton=$(source==='assets'?'#refresh-assets':'#refresh-logs'),refreshMeta=$('#log-refresh-meta'),buttonText=source==='assets'?'刷新素材':'立即刷新';
  if(manual&&refreshButton){refreshButton.disabled=true;refreshButton.textContent='刷新中…'}
  try{
    const p=await api(`/api/projects/${currentProjectId}${manual?'':'/live'}`);
    if(!manual&&p.status!==currentProjectStatus&&['caption_review_ready','review_ready'].includes(p.status)){await show(currentProjectId);return}
    currentProjectStatus=p.status;
    const status=$('#project-status'),logs=$('#project-logs'),stage=$('#control-stage'),item=$('#control-item'),buttons=$('#control-buttons'),workers=$('#worker-status'),error=$('#project-error');
    if(status)status.textContent=displayState(p);if(logs){const openLogDetails=new Set([...logs.querySelectorAll('details[open][data-log-detail]')].map(detail=>detail.dataset.logDetail));logs.innerHTML=logsHtml(p.logs);logs.querySelectorAll('details[data-log-detail]').forEach(detail=>detail.open=openLogDetails.has(detail.dataset.logDetail))}if(stage)stage.textContent=p.control?.stage||statusNames[p.status]||p.status;if(item)item.textContent=p.control?.item||'等待操作';if(error)error.innerHTML=p.error?`<div class="error">${esc(p.error)}</div>`:'';
    if(buttons){buttons.innerHTML=controlsHtml(p);wireControls(p)}if(workers)workers.innerHTML=workersHtml(p);
    const captionsNowComplete=manual?captionsComplete(p.assets):Boolean(p.captions_complete);
    if(manual)refreshAssetList(p.assets,false);
    else if(captionsNowComplete&&!allAssetCaptionsComplete){const full=await api('/api/projects/'+currentProjectId);refreshAssetList(full.assets,false)}
    allAssetCaptionsComplete=captionsNowComplete;
    if(refreshMeta)refreshMeta.textContent=`每 60 秒自动刷新 · 最近 200 条 · ${new Date().toLocaleTimeString()} 已刷新`;
  }catch(e){if(refreshMeta)refreshMeta.textContent='刷新失败，将在下一轮重试';if(manual)alert(e.message)}
  finally{if(manual&&refreshButton){refreshButton.disabled=false;refreshButton.textContent=buttonText}}
}

$('#scan').onclick=async()=>{const b=$('#scan'),old=b.textContent;b.disabled=true;b.textContent='正在扫描…';try{await api('/api/scan',{method:'POST'});await load()}catch(e){alert(e.message)}finally{b.disabled=false;b.textContent=old}};
load();setInterval(()=>refreshLive(false),60000);setInterval(load,30000);
