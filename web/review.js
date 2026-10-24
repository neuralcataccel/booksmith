'use strict';
(() => {
  const pron={contextPreference:true,book:null,nonce:0,load:0,rows:[],signature:'',timer:null,preview:null,previewNonce:0,nextOffset:0};
  const qa={job:null,nonce:0,load:0,signature:'',timer:null,seek:0};
  const pronDialog=$('pronunciation-dialog'), qualityDialog=$('quality-dialog');
  const supported=()=>state.config?.voices.find(v=>v.id===$('voice').value)?.lang==='en-us';
  const friendly={applied:'Applied',already_correct:'The usual reading already matches',dictionary_override:'Your dictionary takes precedence',
    number_alignment:'Kept the usual reading: this passage contains numbers',unaligned:'Kept the usual reading: occurrence alignment was uncertain',
    unsupported_language:'Kept the usual reading: voice language is unsupported',unrecognized_baseline:'Kept the usual reading: baseline phones were not recognized',
    unsupported_phones:'Kept the usual reading: unsupported phones',low_transcript_agreement:'Transcript differs from the expected words',
    possible_missing_ending:'The transcript may be missing the ending',silent_audio:'The sample is silent',possible_clipping:'Possible audio clipping',
    unusually_short_audio:'Audio is unusually short for this text',unusually_long_audio:'Audio is unusually long for this text',invalid_duration:'Invalid duration'};
  const describe=s=>friendly[s]||String(s).replaceAll('_',' ');
  const position=s=>`${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;
  function voiceState(){
    const ok=supported(),input=$('use-context-pronunciations');
    input.disabled=!ok;input.checked=ok&&pron.contextPreference;
    $('context-voice-help').textContent=ok?'Approved readings apply only to their saved source occurrences.':'Context-guided readings currently support American English voices. You can still use this narrator normally.';
  }
  for(const id of ['voice','reading-profile'])$(id).addEventListener('change',voiceState);
  document.addEventListener('booksmith-ready',voiceState);if(state.config)voiceState();
  $('use-context-pronunciations').addEventListener('change',()=>{pron.contextPreference=$('use-context-pronunciations').checked;invalidatePreview();});
  function highlight(context){const {text,target}=context;return escapeHTML(text.slice(0,target.start))+'<mark>'+escapeHTML(text.slice(target.start,target.end))+'</mark>'+escapeHTML(text.slice(target.end));}
  async function loadPronunciations(){
    if(!pron.book||!pronDialog.open)return;
    const nonce=pron.nonce,load=++pron.load,chapter=$('pronunciation-chapter').value;
    try{
      const opts=selectedOptions();const data=await api(`/api/books/${pron.book}/context-pronunciations?chapter=${encodeURIComponent(chapter)}&skip_notes=${opts.skip_notes}&skip_frontmatter=${opts.skip_frontmatter}`);
      if(nonce!==pron.nonce||load!==pron.load||!pronDialog.open)return;
      const active=data.analysis&&['queued','running'].includes(data.analysis.status);
      const c=data.coverage, offset=c?.offset||0;
      pron.nextOffset=c?.next_offset??(c&&c.checked===c.selected&&!c.unavailable&&offset+c.selected<c.found?offset+c.selected:0);
      const finished=c&&!c.unavailable&&c.checked===c.selected&&offset+c.selected>=c.found;
      $('find-pronunciations').disabled=active||finished||!supported()||!data.available;
      $('find-pronunciations').textContent=finished?'Suggestions up to date':pron.nextOffset?'Check next batch':'Find suggestions';
      let status=active?`${data.analysis.message} · ${data.analysis.done_chunks}/${data.analysis.total_chunks||'…'}`:'';
      if(!active&&c){const count=data.items.filter(r=>r.suggestion).length;status=`${count} of ${c.found} possible readings have suggestions. `;if(c.selected)status+=`Latest batch: occurrences ${offset+1}–${offset+c.selected}, ${c.checked} checked. `;status+=(c.billing_state==='unreconciled'?'Billing is pending reconciliation.':`$${Number(c.new_cost||0).toFixed(6)} billed for this batch.`);if(pron.nextOffset)status+=` Check the next batch to continue; at most ${c.limit} per request.`;if(c.unavailable)status+=' Some suggestions are unavailable; the usual reading is preserved.';}
      if(!data.available)status+=' Hosted checks are currently unavailable. Existing choices and normal conversion still work.';
      if(!supported())status+=' Choose an American voice to check or preview readings.';
      $('pronunciation-status').textContent=status||'Find suggestions for this chapter. Each choice needs your review.';
      if(active)return;
      const signature=JSON.stringify(data.items);if(signature===pron.signature)return;pron.signature=signature;pron.rows=data.items;
      $('pronunciation-list').innerHTML=data.items.length?data.items.map(row=>{
        const proposed=row.suggestion?.recommendation?.choice??row.suggestion?.answer.choice;
        const uncertain=!row.suggestion||proposed==='unknown'||row.suggestion.unique_probability<.9||(row.suggestion.confirmation?.unique_probability??1)<.9;
        const selected=row.state==='approved'?row.choice:row.state==='rejected'?'':!uncertain&&proposed&&proposed!=='unknown'?proposed:'';
        const suggestedLabel=row.state==='pending'&&row.candidates.find(c=>c.id===proposed)?.label;
        const label=row.state==='approved'?'Approved for new conversions':row.state==='rejected'?'Keeping the usual reading':row.suggestion?.recommendation?.reason==='context_disagreement'?'Context suggests different readings. Listen before choosing.':uncertain?'Context is uncertain. Review before choosing.':'Suggested reading — not yet approved';
        return `<article class="pronunciation-card" data-pronunciation="${row.id}"><h3>${escapeHTML(row.word)} <small>${escapeHTML(label)}</small></h3><blockquote>${highlight(row.context)}</blockquote>${suggestedLabel?`<p class="field-help">Model's preferred reading: ${escapeHTML(suggestedLabel)}. ${uncertain?'Context is uncertain; choose after listening.':'Listen before saving.'}</p>`:''}<label class="field-label" for="choice-${row.id}">READING FOR THIS OCCURRENCE</label><select id="choice-${row.id}" ${row.stale?'disabled':''}><option value="">Keep the usual reading</option>${row.candidates.map(c=>`<option value="${escapeHTML(c.id)}" ${c.id===selected?'selected':''}>${escapeHTML(c.label)}</option>`).join('')}</select><div class="review-actions"><button class="secondary" data-pron-action="usual" data-pron-id="${row.id}">Hear usual</button><button class="secondary" data-pron-action="choice" data-pron-id="${row.id}">Hear choice</button><button class="primary" data-pron-action="save" data-pron-id="${row.id}" ${row.stale?'disabled':''}>Save choice</button></div></article>`;
      }).join(''):'<p class="field-help">No saved suggestions in this chapter. Run a check to look for supported ambiguous words.</p>';
    }catch(error){if(nonce===pron.nonce)$('pronunciation-status').textContent=error.message;}
  }
  async function pollPronunciationPreview(){
    const id=pron.preview,nonce=pron.nonce;if(!id||!pronDialog.open)return;
    try{
      const job=await api(`/api/jobs/${id}`);if(id!==pron.preview||nonce!==pron.nonce||!pronDialog.open)return;
      if(job.status==='ready'){
        const player=$('pronunciation-preview-player');
        if(!player.src.endsWith(job.preview_url)){player.src=job.preview_url;player.classList.remove('hidden');player.play().catch(()=>{});}
        const outcomes=job.pronunciation_outcomes||[];
        $('pronunciation-preview-status').textContent='Preview ready. '+outcomes.map(o=>describe(o.status)).join('. ');
      }else if(['failed','cancelled'].includes(job.status)){$('pronunciation-preview-status').textContent=job.error||'Preview interrupted.';pron.preview=null;}
      else $('pronunciation-preview-status').textContent='Preparing this passage in your selected voice…';
    }catch(error){$('pronunciation-preview-status').textContent=error.message;}
  }
  $('open-pronunciations').addEventListener('click',async()=>{
    if(!state.selected?.available)return;
    voiceState();pron.book=state.selected.id;pron.nonce++;pron.signature='';pron.rows=[];pron.preview=null;pron.nextOffset=0;$('find-pronunciations').disabled=true;
    const nonce=pron.nonce;pron.bodyOverflow=document.body.style.overflow;document.body.style.overflow='hidden';pronDialog.showModal();$('pronunciation-title').textContent=state.selected.title;
    $('pronunciation-voice').textContent='Preview voice: '+state.config.voices.find(v=>v.id===$('voice').value).name;
    $('pronunciation-list').innerHTML='';$('pronunciation-status').textContent='Loading chapters…';$('pronunciation-chapter').innerHTML='';
    try{
      const options=selectedOptions();const data=await api(`/api/books/${pron.book}/chapters?skip_notes=${options.skip_notes}&skip_frontmatter=${options.skip_frontmatter}`);
      if(nonce!==pron.nonce||!pronDialog.open)return;
      $('pronunciation-chapter').innerHTML=data.items.map(c=>`<option value="${c.index}">${escapeHTML(c.title)}</option>`).join('');
      $('pronunciation-chapter').value=$('chapter-selection').value||'0';
      await loadPronunciations();clearInterval(pron.timer);pron.timer=setInterval(()=>{loadPronunciations();pollPronunciationPreview();},2000);
    }catch(error){$('pronunciation-status').textContent=error.message;}
  });
  $('close-pronunciations').addEventListener('click',()=>pronDialog.close());
  pronDialog.addEventListener('close',()=>{document.body.style.overflow=pron.bodyOverflow||'';pron.nonce++;clearInterval(pron.timer);pron.preview=null;$('pronunciation-preview-player').pause();});
  $('pronunciation-chapter').addEventListener('change',()=>{pron.signature='';pron.rows=[];pron.nextOffset=0;$('find-pronunciations').disabled=true;$('pronunciation-list').innerHTML='';$('pronunciation-preview-player').pause();loadPronunciations();});
  $('refresh-pronunciations').addEventListener('click',loadPronunciations);
  $('find-pronunciations').addEventListener('click',async()=>{
    if(!pron.book)return;const button=$('find-pronunciations');button.disabled=true;
    try{await api(`/api/books/${pron.book}/context-pronunciations`,{method:'POST',body:JSON.stringify({...selectedOptions(),chapter:Number($('pronunciation-chapter').value),offset:pron.nextOffset})});await loadPronunciations();}
    catch(error){$('pronunciation-status').textContent=error.message;button.disabled=false;}
  });
  $('pronunciation-list').addEventListener('click',async event=>{
    const button=event.target.closest('[data-pron-action]');if(!button)return;
    const row=pron.rows.find(r=>r.id===button.dataset.pronId);if(!row)return;
    const choice=$('choice-'+row.id).value||null;button.disabled=true;
    try{
      if(button.dataset.pronAction==='save'){
        await api(`/api/pronunciation-reviews/${row.id}/decision`,{method:'POST',body:JSON.stringify({expected_revision:row.revision,choice})});
        invalidatePreview();toast('Choice saved for new previews, conversions and repairs.');await loadPronunciations();
      }else{
        const token=++pron.previewNonce;$('pronunciation-preview-player').pause();
        const job=await api(`/api/pronunciation-reviews/${row.id}/preview`,{method:'POST',body:JSON.stringify({...selectedOptions(),expected_revision:row.revision,choice:button.dataset.pronAction==='usual'?null:choice})});
        if(token!==pron.previewNonce||!pronDialog.open)return;pron.preview=job.id;await pollPronunciationPreview();
      }
    }catch(error){toast(error.message,true);}finally{button.disabled=false;}
  });

  function qualityEntry(item,jid,canRepair){return `<article class="quality-passage ${item.needs_listening?'flagged':''}"><h3>${escapeHTML(item.title)} · ${position(item.start_seconds)}</h3>${item.reasons?.length?`<p>${item.reasons.map(r=>escapeHTML(describe(r))).join(' · ')}</p>`:''}<blockquote>${escapeHTML(item.text||'')}</blockquote>${item.transcript?`<details><summary>Transcript comparison</summary><p>${escapeHTML(item.transcript)}</p><p>Word-sequence agreement: ${Math.round(item.sequence_similarity*100)}%. This is not a pronunciation score.</p></details>`:''}<div class="review-actions"><button class="secondary" data-listen-chapter="${item.chapter}" data-listen-time="${item.start_seconds}" data-listen-title="${escapeHTML(item.title)}">Hear passage</button>${canRepair?`<button class="text-button" data-quality-repair="${item.chapter}">Repair chapter</button>`:''}</div></article>`;}
  async function loadQuality(){
    if(!qa.job||!qualityDialog.open)return;
    const nonce=qa.nonce,load=++qa.load;
    try{
      const report=await api(`/api/jobs/${qa.job}/quality`);if(nonce!==qa.nonce||load!==qa.load||!qualityDialog.open)return;
      $('quality-title').textContent=report.title;
      const active=['queued','running'].includes(report.state);$('run-quality').disabled=active||report.state==='complete';
      $('run-quality').textContent=active?'Checks in progress':report.state==='complete'?'Checks up to date':'Run sample checks';
      $('quality-status').textContent=report.state==='complete'?`${report.checked_passages} of ${report.total_passages} passages transcribed · ${report.flagged_passages} passages need listening. Other passages remain unchecked by transcription.`:active?'Local sample checks are waiting or running. Your completed audio remains available.':report.message||'This edition has not had completed sample checks.';
      const signature=JSON.stringify(report);if(signature===qa.signature)return;qa.signature=signature;
      if(report.state!=='complete'){$('quality-results').innerHTML='';return;}
      const entries=new Map((report.samples||[]).map(s=>[`${s.chapter}/${s.chunk}`,{...s}]));
      for(const item of report.duration_checks||[]){const key=`${item.chapter}/${item.chunk}`;const existing=entries.get(key);entries.set(key,{...item,...existing,needs_listening:true,reasons:[...new Set([...(existing?.reasons||[]),...item.reasons])]});}
      for(const item of report.corrections||[]){if(['applied','already_correct','dictionary_override'].includes(item.status))continue;const key=`${item.chapter}/${item.chunk}`,existing=entries.get(key);entries.set(key,{...item,...existing,needs_listening:true,reasons:[...new Set([...(existing?.reasons||[]),item.status])]});}
      const ordered=[...entries.values()].sort((a,b)=>Number(b.needs_listening)-Number(a.needs_listening)||a.chapter-b.chapter||a.chunk-b.chunk);
      const corrections=report.corrections||[],applied=corrections.filter(c=>['applied','already_correct'].includes(c.status)).length;
      $('quality-results').innerHTML=`<p class="field-help">Checks completed ${new Date(report.finished*1000).toLocaleString()}. Audio screens ran on ${report.audio_screen_checked||0} sampled passages. ${corrections.length?`${applied}/${corrections.length} reviewed readings applied or already matched; other outcomes are recorded below.`:''}</p>`+ordered.map(x=>qualityEntry(x,qa.job,report.can_repair)).join('')+`<p class="field-help">${(report.limitations||[]).map(escapeHTML).join(' ')}</p>`;
    }catch(error){if(nonce===qa.nonce)$('quality-status').textContent=error.message;}
  }
  document.addEventListener('click',event=>{
    const button=event.target.closest('[data-quality-job]');if(!button)return;
    qa.job=button.dataset.qualityJob;qa.nonce++;qa.signature='';qa.bodyOverflow=document.body.style.overflow;document.body.style.overflow='hidden';qualityDialog.showModal();$('quality-results').innerHTML='';$('quality-title').textContent='Loading listening checks…';
    loadQuality();clearInterval(qa.timer);qa.timer=setInterval(loadQuality,2500);
  });
  $('close-quality').addEventListener('click',()=>qualityDialog.close());
  qualityDialog.addEventListener('close',()=>{document.body.style.overflow=qa.bodyOverflow||'';qa.nonce++;qa.seek++;clearInterval(qa.timer);$('quality-player').pause();});
  $('run-quality').addEventListener('click',async()=>{try{await api(`/api/jobs/${qa.job}/quality`,{method:'POST'});await loadQuality();}catch(error){$('quality-status').textContent=error.message;}});
  $('quality-results').addEventListener('click',async event=>{
    const repairButton=event.target.closest('[data-quality-repair]');
    if(repairButton){const job=qa.job,chapter=repairButton.dataset.qualityRepair;qualityDialog.close();await openRepair(job);$('repair-chapter').value=chapter;return;}
    const button=event.target.closest('[data-listen-chapter]');if(!button)return;
    const player=$('quality-player'),nonce=++qa.seek;
    const url=`/api/jobs/${qa.job}/quality-audio?chapter=${encodeURIComponent(button.dataset.listenChapter)}`;
    const seek=()=>{if(nonce!==qa.seek||!qualityDialog.open)return;player.currentTime=Math.max(0,Number(button.dataset.listenTime)-.5);player.play().catch(()=>{});};
    player.classList.remove('hidden');$('quality-player-label').textContent=`${button.dataset.listenTitle} · around ${position(Number(button.dataset.listenTime))}`;
    if(player.src!==new URL(url,location.href).href){player.src=url;player.addEventListener('loadedmetadata',seek,{once:true});player.load();player.play().catch(()=>{});}else if(player.readyState>=1)seek();else{player.addEventListener('loadedmetadata',seek,{once:true});player.load();player.play().catch(()=>{});}
  });
})();
