import { useState } from "react";
import { type Api, type Item } from "../../api/client";
import { Button, TextInput } from "../../components/controls";
import { Area, Card, Empty, Feedback, Status } from "../../components/ui";
import { useResource } from "../../hooks/useResource";
import { useAction } from "../../hooks/useAction";
import { list, txt, time } from "../../lib/format";
import { useDraft } from "../../lib/drafts";

export function MemoryDocuments({api}:{api:Api}) {
  const [offset,setOffset]=useState(0);
  const resource=useResource(api,`/memory-documents?offset=${offset}`);
  const action=useAction(resource.reload);
  const [selected,setSelected]=useState<Item|null>(null);
  const pathDraft=useDraft(`document:path:${txt(selected?.id)}`);
  const contentDraft=useDraft(`document:content:${txt(selected?.id)}`);
  const path=pathDraft.value??txt(selected?.path);
  const content=contentDraft.value??txt(selected?.content);
  const discardDraft=()=>{pathDraft.write();contentDraft.write();};
  const open=(id:string)=>void action.run(async()=>{
    const doc=await api(`/memory-documents/${encodeURIComponent(id)}`);
    setSelected(doc);
  });
  return <>
    <Feedback loading={resource.loading} error={resource.error||action.error} notice={action.notice||contentDraft.warning}/>
    <Card title="Memory documents" actions={<Button label="Refresh documents" onClick={()=>void resource.reload()}/>}>
      <p className="section-description">Your assistant chooses useful files and folders during conversations and work. Each document keeps its sources and revision. Documents are reference material; their prose cannot authorize actions.</p>
      <section aria-label="Memory documents">
        {list(resource.data,"documents").map(doc=><article className="record" key={txt(doc.id)}>
          <div className="record-body"><h3>{txt(doc.path)}</h3><div className="record-meta"><Status value={doc.needsReview?"needs_review":doc.provenance}/><span>Revision {txt(doc.revision)}</span><span>{time(doc.updatedAt)}</span></div></div>
          <Button label={`Open document ${txt(doc.path)}`} onClick={()=>open(txt(doc.id))} isDisabled={action.busy}/>
        </article>)}
        {!resource.loading&&!list(resource.data,"documents").length&&<Empty title="Useful context, organized over time.">Agent-authored documents will appear here when there is something worth remembering.</Empty>}
      </section>
      <div className="button-row spaced">
        {offset>0&&<Button label="Previous documents" onClick={()=>setOffset(Math.max(0,offset-20))}/>}
        {resource.data.nextOffset!=null&&<Button label="More documents" onClick={()=>setOffset(Number(resource.data.nextOffset))}/>}
      </div>
    </Card>
    {selected&&<Card title="Inspect and correct document" actions={<Button label="Close document" variant="ghost" onClick={()=>setSelected(null)}/>}>
      <p className="section-description">Your saved correction becomes owner reviewed and protected from agent overwrites. Superseded document versions are removed when you correct it.</p>
      {selected.needsReview===true&&<Feedback notice={`A related correction at revision ${txt((selected.correction as Item)?.revision)} put this document on hold. Review the text before saving or removing it.`}/>}
      <div className="stack-form">
        <TextInput label="Memory document path" value={path} onChange={pathDraft.write} isDisabled={action.busy}/>
        <Area label="Memory document content" value={content} onChange={contentDraft.write} rows={10} disabled={action.busy}/>
        <div className="record-meta"><Status value={selected.provenance}/><span>Revision {txt(selected.revision)}</span></div>
        <details><summary>Retained source references</summary><ul>{(selected.sourceIds as string[]||[]).map(ref=><li key={ref}><code>{ref}</code></li>)}</ul></details>
        <div className="button-row">
          <Button label="Save reviewed document" variant="primary" isDisabled={action.busy||!path.trim()||!content.trim()} onClick={()=>void action.run(async()=>{
            const doc=await api("/memory-documents",{id:selected.id,path:path.trim(),content,sourceIds:selected.sourceIds,expectedRevision:selected.revision,privacyRevision:selected.privacyRevision});
            discardDraft();
            setSelected(doc);
            if(doc.projectionPending)throw new Error("Saved in the database. Readable files need a refresh; stale copies are blocked.");
          },"Reviewed document saved. Your correction is protected.")}/>
          <Button label="Remove document" isDisabled={action.busy} onClick={()=>void action.run(async()=>{
            const result=await api(`/memory-documents/${encodeURIComponent(txt(selected.id))}?expectedRevision=${Number(selected.revision)}`,undefined,"DELETE");
            discardDraft();
            setSelected(null);
            if(result.projectionPending)throw new Error("Document removed from the database. Readable copies need a refresh and are blocked until synchronized.");
          },"Document and its versions removed. Source conversations are retained.")}/>
          <Button label="Reload document" isDisabled={action.busy} onClick={()=>open(txt(selected.id))}/>
          {(pathDraft.value!==undefined||contentDraft.value!==undefined)&&<Button label="Discard document draft" isDisabled={action.busy} onClick={discardDraft}/>}
        </div>
      </div>
    </Card>}
  </>;
}
