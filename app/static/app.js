const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,opt){const r=await fetch(url,opt);if(!r.ok)throw new Error(await r.text());return r.json()}

async function load(){
  const [h,ps]=await Promise.all([api('/api/health'),api('/api/projects')]);
  $('#health').textContent=`D盘可用 ${h.free_gib} GiB`;$('#health').className=h.free_gib<h.minimum_free_gib?'bad':'ok';
  $('#projects').innerHTML=ps.map(p=>`<button class="project" data-id="${p.id}"><b>${esc(p.title)}</b><span>${esc(p.status)}</span></button>`).join('')||'<p class="muted">尚无项目</p>';
  document.querySelectorAll('.project').forEach(b=>b.onclick=()=>show(b.dataset.id));
}

function exportHtml(e){
  const files=e.path?(()=>{try{return JSON.parse(e.path)}catch{return[e.path]}})():[];
  const previews=files.map((_,i)=>`<video controls preload="metadata" src="/api/exports/${e.id}/files/${i}"></video>`).join('');
  return `<li><b>${esc(e.version)}</b> · ${esc(e.format)} · ${esc(e.status)} ${e.status==='review_ready'?`<button data-approve="${e.id}">批准锁定</button>`:''}${previews}</li>`;
}

async function show(id){
  const p=await api('/api/projects/'+id),uploaded=new Set(p.uploads.map(x=>x.platform)),story=p.settings?.story_plan;
  $('#detail').innerHTML=`
    <div class="title"><div><h2>${esc(p.title)}</h2><span class="tag">${esc(p.status)}</span></div><small>${p.assets.length} 个视频</small></div>
    ${p.error?`<div class="error">${esc(p.error)}</div>`:''}
    <div class="cards">
      <div class="card"><h3>制作设定</h3><label>模式<select id="mode"><option value="existing">已有素材重建故事</option><option value="scripted">脚本驱动拍摄</option></select></label><label>备注<textarea id="notes" placeholder="风格、重要事件、不要使用的内容……">${esc(p.notes)}</textarea></label><button id="save">保存设定</button></div>
      <div class="card"><h3>输出版本</h3><button data-export="long_16x9">请求长篇 16:9</button><button data-export="short_9x16">请求短篇 9:16</button><ul>${p.exports.map(exportHtml).join('')||'<li>尚无输出</li>'}</ul></div>
    </div>
    <div class="card"><h3>AI剪辑方案</h3>${story?`<p>${esc(story.summary||story.title||'')}</p><details><summary>查看完整可编辑时间线</summary><pre>${esc(JSON.stringify(story,null,2))}</pre></details>`:'<p class="muted">等待音频、字幕和画面分析完成。</p>'}</div>
    <div class="card"><h3>审核修改</h3><p class="muted">提交后AI会重写相关时间线，处理完成后再请求新版本。</p><div class="row"><select id="kind"><option value="edit">剪辑</option><option value="subtitle">字幕/翻译</option><option value="audio">音频</option><option value="music">音乐</option><option value="shot">替换镜头</option></select><input id="revision" placeholder="例如：第三章压缩到5分钟，锁定开头"><button id="send">追加修改</button></div><ul>${p.revisions.map(r=>`<li><b>${esc(r.kind)}</b> ${esc(r.body)} · ${esc(r.status)}</li>`).join('')||'<li>暂无修改意见</li>'}</ul></div>
    <div class="card"><h3>素材与代理预览</h3><div class="assets">${p.assets.map(a=>{let x={};try{x=JSON.parse(a.analysis||'{}')}catch{};return`<div class="asset"><div><b>${esc(a.filename)}</b><span>${Math.round(a.duration||0)}秒 · ${a.width}×${a.height} · ${esc(a.codec)}</span>${x.transcript?`<p>${esc(x.transcript.slice(0,300))}</p>`:''}</div><video controls preload="none" src="/api/assets/${a.id}/proxy"></video></div>`}).join('')}</div></div>
    <div class="card"><h3>四平台上传确认</h3><p class="muted">四项全部确认后开始计算原片/中间文件14天保留期。</p>${['youtube','bilibili','douyin','xiaohongshu'].map(x=>`<button data-upload="${x}" ${uploaded.has(x)?'disabled':''}>${x} ${uploaded.has(x)?'✓':''}</button>`).join('')}</div>`;
  $('#mode').value=p.mode;
  $('#save').onclick=async()=>{await api('/api/projects/'+p.id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:$('#mode').value,notes:$('#notes').value,settings:p.settings})});show(p.id)};
  $('#send').onclick=async()=>{if(!$('#revision').value.trim())return;await api(`/api/projects/${p.id}/revisions`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:$('#kind').value,body:$('#revision').value})});show(p.id)};
  document.querySelectorAll('[data-export]').forEach(b=>b.onclick=async()=>{await api(`/api/projects/${p.id}/exports/${b.dataset.export}`,{method:'POST'});show(p.id)});
  document.querySelectorAll('[data-approve]').forEach(b=>b.onclick=async()=>{await api(`/api/exports/${b.dataset.approve}/approve`,{method:'POST'});show(p.id)});
  document.querySelectorAll('[data-upload]').forEach(b=>b.onclick=async()=>{await api(`/api/projects/${p.id}/uploads/${b.dataset.upload}`,{method:'POST'});show(p.id)});
}

$('#scan').onclick=async()=>{await api('/api/scan',{method:'POST'});load()};load();setInterval(load,30000);
