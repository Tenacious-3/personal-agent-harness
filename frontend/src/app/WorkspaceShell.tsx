import { useEffect, useRef, useState, type ReactNode } from "react";
import { type Api, type Item } from "../api/client";
import { Icon } from "../components/Icon";
import { CompanionPanel } from "../features/activity/CompanionPanel";
import { SettingsDialog } from "../features/settings/SettingsDialog";
import { descriptions, navigationGroups, type View } from "./navigation";

const primary: {view:View;icon:string}[] = [
  {view:"Chat",icon:"chat"},{view:"History",icon:"search"},{view:"Memory",icon:"memory"},
  {view:"Goals",icon:"goals"},{view:"Tasks",icon:"tasks"},{view:"Workflows",icon:"workflows"},
  {view:"Activity",icon:"activity"},{view:"Connections",icon:"connections"},
];
const secondary: View[] = ["Inbox","Approvals","Schedules","Browser","Files","People","Repairs","Evidence"];
const pageIcons: Partial<Record<View,string>> = {Chat:"chat",History:"search",Memory:"memory",Goals:"goals",Tasks:"tasks",Workflows:"workflows",Activity:"activity",Connections:"connections",Inbox:"inbox",Approvals:"shield",Schedules:"calendar",Browser:"browser",Files:"folder",People:"people",Repairs:"repair",Evidence:"shield"};

export function WorkspaceShell({api,runtime,view,onNavigate,mode,onModeChange,onDisconnect,children}: {
  api:Api;runtime:Item;view:View;onNavigate:(view:View)=>void;mode:"light"|"dark";
  onModeChange:(mode:"light"|"dark")=>void;onDisconnect:()=>void;children:ReactNode;
}) {
  const [settingsOpen,setSettingsOpen]=useState(false);
  const [moreOpen,setMoreOpen]=useState(false);
  const [pagesOpen,setPagesOpen]=useState(false);
  const [pageQuery,setPageQuery]=useState("");
  const pagesSearch=useRef<HTMLInputElement>(null);
  const pagesTrigger=useRef<HTMLButtonElement>(null);
  const [panelOpen,setPanelOpen]=useState(()=>window.matchMedia("(min-width: 1180px)").matches);
  const more=useRef<HTMLDivElement>(null);
  useEffect(()=>{
    const query=window.matchMedia("(min-width: 1180px)");
    const change=()=>setPanelOpen(query.matches);
    query.addEventListener("change",change);
    return()=>query.removeEventListener("change",change);
  },[]);
  useEffect(()=>{
    if(!moreOpen)return;
    const click=(event:PointerEvent)=>{if(!more.current?.contains(event.target as Node))setMoreOpen(false);};
    const key=(event:KeyboardEvent)=>{if(event.key==="Escape")setMoreOpen(false);};
    document.addEventListener("pointerdown",click);document.addEventListener("keydown",key);
    return()=>{document.removeEventListener("pointerdown",click);document.removeEventListener("keydown",key);};
  },[moreOpen]);
  useEffect(()=>{
    if(!pagesOpen)return;
    pagesSearch.current?.focus();
    const key=(event:KeyboardEvent)=>{if(event.key==="Escape"){setPagesOpen(false);pagesTrigger.current?.focus();}};
    document.addEventListener("keydown",key);
    return()=>document.removeEventListener("keydown",key);
  },[pagesOpen]);
  const navigate=(next:View)=>{setMoreOpen(false);setPagesOpen(false);setPageQuery("");onNavigate(next);};
  const matchingGroups=navigationGroups.map(group=>({...group,items:group.items.filter(name=>`${name} ${descriptions[name]}`.toLowerCase().includes(pageQuery.toLowerCase().trim()))}));
  return <div className={`workspace-shell ${view==="Chat"?"workspace-chat":"workspace-page-view"}`} data-view={view.toLowerCase()}>
    <aside className="workspace-rail">
      <button className="rail-brand" aria-label="Personal Harness home" title="Personal Harness" onClick={()=>navigate("Chat")}><Icon name="sparkles" size={27}/></button>
      <nav aria-label="Main navigation" className="rail-navigation">
        {primary.map(item=><button key={item.view} className={`rail-button ${view===item.view?"active":""}`} aria-label={item.view} title={item.view} aria-current={view===item.view?"page":undefined} onClick={()=>navigate(item.view)}><Icon name={item.icon}/></button>)}
        <div ref={more} className="rail-more">
          <button className={`rail-button ${secondary.includes(view)?"active":""}`} aria-label="More tools" title="More tools" aria-expanded={moreOpen} onClick={()=>setMoreOpen(!moreOpen)}><Icon name="grid"/></button>
          {moreOpen&&<div className="rail-popover" aria-label="More tools navigation"><span className="rail-popover-heading">Workspace tools</span>{secondary.map(name=><button key={name} aria-current={view===name?"page":undefined} onClick={()=>navigate(name)}>{name}<Icon name="arrow-right" size={15}/></button>)}</div>}
        </div>
      </nav>
      <div className="rail-bottom">
        <button className="rail-button" title="Settings" aria-label="Settings" onClick={()=>{setMoreOpen(false);setSettingsOpen(true);}}><Icon name="settings"/></button>
        <button className="rail-button" title="Disconnect workspace" aria-label="Disconnect workspace" onClick={onDisconnect}><Icon name="logout"/></button>
      </div>
    </aside>
    {view!=="Chat"&&<>
      {pagesOpen&&<button className="workspace-pages-backdrop" aria-label="Dismiss pages" onClick={()=>{setPagesOpen(false);pagesTrigger.current?.focus();}}/>}
      <aside id="workspace-pages" className={`workspace-pages ${pagesOpen?"is-open":""}`} aria-label="Workspace pages">
        <div className="workspace-pages-title"><strong>Your workspace</strong><button aria-label="Close pages" onClick={()=>{setPagesOpen(false);pagesTrigger.current?.focus();}}><Icon name="close" size={18}/></button></div>
        <label className="workspace-pages-search"><Icon name="search" size={17}/><input ref={pagesSearch} aria-label="Find a page" placeholder="Find a page" value={pageQuery} onChange={event=>setPageQuery(event.target.value)}/></label>
        <nav aria-label="Workspace navigation">
          {matchingGroups.map(group=>group.items.length>0&&<section key={group.label}><h2>{group.label}</h2>{group.items.map(name=><button key={name} aria-label={`Open ${name} page`} aria-current={view===name?"page":undefined} onClick={()=>navigate(name)}><Icon name={pageIcons[name]||"grid"} size={18}/><span>{name}</span></button>)}</section>)}
          {!matchingGroups.some(group=>group.items.length)&&<p className="workspace-pages-empty">No pages found. Try a shorter name.</p>}
        </nav>
        <div className="workspace-pages-note"><span className="connection-dot connected"/>Private local workspace</div>
      </aside>
    </>}
    <main className="workspace-main">
      {view!=="Chat"&&<header className="workspace-page-heading"><div><button ref={pagesTrigger} className="workspace-pages-toggle" aria-label="Show pages" aria-controls="workspace-pages" aria-expanded={pagesOpen} onClick={()=>setPagesOpen(!pagesOpen)}><Icon name="panel" size={18}/>Pages</button><h1>{view}</h1><p>{descriptions[view]}</p></div><button className="workspace-back" aria-label="Back to chat" onClick={()=>navigate("Chat")}><Icon name="chat" size={17}/><span>Back to chat</span></button></header>}
      <div className={`workspace-stage ${view==="Chat"&&panelOpen?"with-companion":""}`}>
        <div className="workspace-route">
          {view==="Chat"&&<div className="workspace-chat-actions"><span className="runtime-chip"><span/>Local workspace</span><button aria-label={panelOpen?"Hide activity panel":"Show activity panel"} title="Assistant activity" onClick={()=>setPanelOpen(!panelOpen)}><Icon name="panel" size={19}/></button></div>}
          {children}
        </div>
        {view==="Chat"&&panelOpen&&<CompanionPanel api={api} runtime={runtime} onNavigate={navigate} onClose={()=>setPanelOpen(false)}/>}
      </div>
    </main>
    <SettingsDialog api={api} open={settingsOpen} onClose={()=>setSettingsOpen(false)} mode={mode} onModeChange={onModeChange} onNavigate={navigate} onDisconnect={onDisconnect}/>
  </div>;
}
