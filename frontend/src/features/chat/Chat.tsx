import { useEffect, useRef, useState } from "react";
import { Button } from "../../components/controls";
import { type Api, type Item } from "../../api/client";
import { txt, list, obj } from "../../lib/format";
import { Feedback } from "../../components/ui";
import { useDraft } from "../../lib/drafts";

const selectionKey = "assistant.currentChat";
const activeTurn = (turn: Item | null) => turn?.status === "queued" || turn?.status === "running";
function savedSelection() {
  try { return sessionStorage.getItem(selectionKey) || ""; } catch { return ""; }
}

export function Chat({ api, model }: { api: Api; model: string }) {
  const [sessions, setSessions] = useState<Item[]>([]);
  const [modelConfigured, setModelConfigured] = useState(true);
  const [selected, setSelected] = useState("");
  const [messages, setMessages] = useState<Item[]>([]);
  const [archived, setArchived] = useState(false);
  const [revision, setRevision] = useState(0);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [loadedSelection, setLoadedSelection] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const draft = useDraft(`chat:${selected}`);
  const requestDraft = useDraft(`chat-request:${selected}`);
  const input = draft.value ?? "";
  const [search, setSearch] = useState("");
  const [chatsOpen, setChatsOpen] = useState(false);
  const [copied, setCopied] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [nextBefore, setNextBefore] = useState("");
  const [context, setContext] = useState<Item | null>(null);
  const [turn, setTurn] = useState<Item | null>(null);
  const [refreshMessages, setRefreshMessages] = useState(0);
  const end = useRef<HTMLDivElement>(null);
  const conversation = useRef<HTMLDivElement>(null);
  const operation = useRef(false);
  const generation = useRef(0);
  const scrollToEnd = useRef(true);
  const current = sessions.find((session) => session.id === selected);
  const loading = loadingSessions || Boolean(selected && loadedSelection !== selected);
  const disabled = busy || loading;
  const preparingReply = turn?.sessionId === selected && activeTurn(turn);
  const editDraft = (value?: string) => {
    draft.write(value || undefined);
    requestDraft.write();
  };

  useEffect(() => {
    if (requestDraft.value && turn?.sessionId === selected && turn.id === requestDraft.value) editDraft();
  }, [selected, turn?.id, turn?.sessionId, requestDraft.value]);

  useEffect(() => {
    const version = ++generation.current;
    operation.current = false;
    setBusy(false);
    return () => { if (generation.current === version) generation.current++; };
  }, [api]);

  useEffect(() => {
    let stale = false;
    setLoadingSessions(true);
    setError("");
    api(`/chat-sessions${archived ? "?archived=true" : ""}`)
      .then((data) => {
        if (stale) return;
        const available = list(data, "sessions");
        setSessions(available);
        setModelConfigured(data.modelConfigured !== false);
        setSelected((previous) => {
          const preferred = previous || savedSelection();
          return available.some((session) => session.id === preferred)
            ? preferred
            : txt(available.find((session) => session.id === data.defaultSessionId)?.id ?? available[0]?.id);
        });
      })
      .catch((reason: unknown) => { if (!stale) setError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (!stale) setLoadingSessions(false); });
    return () => { stale = true; };
  }, [api, archived, revision]);

  useEffect(() => {
    setMessages([]);
    setContext(null);
    setTurn(null);
    setHasMore(false);
    setRenaming(false);
    setCopied("");
    setLoadedSelection("");
    if (!selected) return;
    try { sessionStorage.setItem(selectionKey, selected); } catch { /* Storage can be unavailable. */ }
    scrollToEnd.current = true;
  }, [api, selected]);

  useEffect(() => {
    if (!selected) return;
    let stale = false;
    let timer: ReturnType<typeof setTimeout>;
    let tracking = false;
    const load = async () => {
      try {
        const data = await api(`/chat-sessions/${encodeURIComponent(selected)}/messages`);
        if (stale) return;
        const latest = data.latestTurn ? obj(data.latestTurn) : null;
        setTurn(latest);
        setContext(latest?.context ? obj(latest.context) : null);
        setMessages(list(data, "messages"));
        setHasMore(data.hasMore === true);
        setNextBefore(txt(data.nextBefore));
        const session = obj(data.session);
        setSessions((previous) => previous.map((item) => item.id === session.id ? session : item));
        tracking = activeTurn(latest);
      } catch (reason) {
        if (!stale) {
          setError(reason instanceof Error ? reason.message : String(reason));
          tracking = true;
        }
      } finally {
        if (!stale) {
          setLoadedSelection(selected);
          if (tracking) timer = setTimeout(() => void load(), 700);
        }
      }
    };
    void load();
    return () => { stale = true; clearTimeout(timer); };
  }, [api, selected, refreshMessages]);

  useEffect(() => {
    if (scrollToEnd.current) end.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [messages.length, busy]);

  const run = async (work: (valid: () => boolean) => Promise<void>) => {
    if (operation.current || disabled) return;
    operation.current = true;
    const version = generation.current;
    const valid = () => generation.current === version;
    setBusy(true);
    setError("");
    try { await work(valid); }
    catch (reason) { if (valid()) setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { if (valid()) { operation.current = false; setBusy(false); } }
  };
  const send = () => {
    const message = input.trim();
    if (!message || !selected || archived || preparingReply) return;
    void run(async (valid) => {
      const requestId = requestDraft.value ?? crypto.randomUUID();
      requestDraft.write(requestId);
      const accepted = await api("/chat-turns", { message, sessionId: selected, requestId });
      if (!valid()) return;
      editDraft();
      scrollToEnd.current = true;
      setTurn(accepted);
      setRefreshMessages((value) => value + 1);
    });
  };
  const cancelReply = () => void run(async (valid) => {
    const cancelled = await api(`/chat-turns/${encodeURIComponent(txt(turn?.id))}/cancel`, {});
    if (!valid()) return;
    setTurn(cancelled);
    setRefreshMessages((value) => value + 1);
  });
  const newChat = () => void run(async (valid) => {
    const session = await api("/chat-sessions", {});
    if (!valid()) return;
    setSessions((previous) => [session, ...previous]);
    setSelected(txt(session.id));
    setArchived(false);
    setSearch("");
    setChatsOpen(false);
    setRevision((value) => value + 1);
  });
  const updateSession = (changes: Item) => void run(async (valid) => {
    const session = await api(`/chat-sessions/${encodeURIComponent(selected)}`, changes, "PATCH");
    if (!valid()) return;
    setSessions((previous) => previous.map((item) => item.id === session.id ? session : item));
    setRenaming(false);
    setRevision((value) => value + 1);
  });
  const older = () => void run(async (valid) => {
    const element = conversation.current;
    const height = element?.scrollHeight ?? 0;
    const top = element?.scrollTop ?? 0;
    const page = await api(`/chat-sessions/${encodeURIComponent(selected)}/messages?before=${encodeURIComponent(nextBefore)}`);
    if (!valid()) return;
    scrollToEnd.current = false;
    setMessages((previous) => [...list(page, "messages"), ...previous]);
    setHasMore(page.hasMore === true);
    setNextBefore(txt(page.nextBefore));
    requestAnimationFrame(() => { if (valid() && element) element.scrollTop = top + element.scrollHeight - height; });
  });
  const filteredSessions = sessions.filter((session) => txt(session.title).toLocaleLowerCase().includes(search.toLocaleLowerCase().trim()));
  const mainChat = filteredSessions.find((session) => session.id === "legacy");
  const sideChats = filteredSessions.filter((session) => session.id !== "legacy");
  const openSession = (id: string) => { setError(""); setSelected(id); setChatsOpen(false); };
  const copyMessage = async (message: Item, index: number) => {
    try {
      await navigator.clipboard.writeText(txt(message.content ?? message.text));
      setCopied(txt(message.id) || String(index));
    } catch { setError("Couldn't copy this message. Select its text and copy it manually."); }
  };
  const sessionButton = (session: Item, main = false) => (
    <button type="button" key={txt(session.id)} className={`session-link ${session.id === selected ? "active" : ""}`}
      aria-label={main ? "Main chat" : `Open chat ${txt(session.title) || "Untitled chat"} (${txt(session.id)})`}
      aria-current={session.id === selected ? "page" : undefined} disabled={disabled} onClick={() => openSession(txt(session.id))}>
      <span className="session-icon" aria-hidden="true">{main ? "◉" : "◇"}</span>
      <span className="session-text"><strong>{main ? "Main chat" : txt(session.title) || "Untitled chat"}</strong><small>{Number(session.messageCount) || 0} messages</small></span>
    </button>
  );

  return (
    <div className={`chat-workspace ${chatsOpen ? "sidebar-open" : ""}`}>
      <aside className="conversation-sidebar" aria-label="Your conversations">
        <div className="conversation-sidebar-heading"><span>Conversations</span><button className="conversation-sidebar-close" type="button" aria-label="Close chats" onClick={() => setChatsOpen(false)}>×</button></div>
        <label className="chat-search"><span aria-hidden="true">⌕</span><input aria-label="Search chats" placeholder="Search chats" value={search} disabled={disabled} onChange={(event) => setSearch(event.target.value)} /></label>
        <label className="visually-hidden">Choose chat
          <select aria-label="Choose chat" value={selected} disabled={disabled || !sessions.length} onChange={(event) => openSession(event.target.value)}>
            {!sessions.length && <option value="">{archived ? "No archived chats" : "No chats yet"}</option>}
            {sessions.map((session) => <option key={txt(session.id)} value={txt(session.id)}>{txt(session.title) || "Untitled chat"} ({Number(session.messageCount) || 0})</option>)}
          </select>
        </label>
        <div className="conversation-list">
          {mainChat && sessionButton(mainChat, true)}
          <div className="conversation-list-heading"><span>{archived ? "Archived chats" : "Side chats"}</span><span>{sideChats.length}</span></div>
          {sideChats.map((session) => sessionButton(session))}
          {!filteredSessions.length && !loading && <p className="no-conversations">{search ? "No matching chats" : archived ? "No archived chats" : "No chats yet"}</p>}
          <button className="new-chat-button" type="button" disabled={disabled} onClick={newChat}><span aria-hidden="true">＋</span> New chat</button>
        </div>
        <button className="archive-toggle" type="button" disabled={disabled} onClick={() => { setArchived(!archived); setRenaming(false); setSearch(""); }}><span aria-hidden="true">▤</span> {archived ? "Show active chats" : "Show archived chats"}</button>
      </aside>
      <section className="chat-panel">
        <div className="chat-topline">
          <button className="chat-sidebar-toggle" type="button" aria-expanded={chatsOpen} disabled={disabled} onClick={() => setChatsOpen(!chatsOpen)}>{chatsOpen ? "Hide chats" : "Show chats"}</button>
          <div className="chat-title"><span className="chat-title-symbol" aria-hidden="true">✳</span><div><h2>{txt(current?.title) || (archived ? "Archived chats" : "Main chat")}</h2><span>{archived ? "Archived conversation" : "Your personal workspace"}</span></div></div>
          <div className="chat-controls">
            <button type="button" disabled={disabled || !current} onClick={() => { setTitle(txt(current?.title)); setRenaming(true); }}>Rename chat</button>
            <button type="button" disabled={disabled || !current || preparingReply} onClick={() => updateSession({ archived: !archived })}>{archived ? "Restore chat" : "Archive chat"}</button>
          </div>
        </div>
        <Feedback loading={loading} error={error} notice={!modelConfigured ? "Set up your model API key in the local workspace configuration, then restart the server to send messages." : undefined} />
        {draft.warning && <Feedback notice={draft.warning} />}
        {renaming && <form className="chat-rename" onSubmit={(event) => { event.preventDefault(); if (title.trim()) updateSession({ title: title.trim() }); }}>
          <label className="native-field">Chat title<input aria-label="Chat title" maxLength={100} value={title} disabled={disabled} onChange={(event) => setTitle(event.target.value)} autoFocus /></label>
          <button type="submit" disabled={disabled || !title.trim()}>Save title</button>
          <button type="button" disabled={disabled} onClick={() => setRenaming(false)}>Cancel</button>
        </form>}
        <div className="conversation" ref={conversation} role="log" aria-label="Conversation" aria-live="polite">
          <div className="conversation-inner">
            {hasMore && <button type="button" className="load-older" disabled={disabled || preparingReply} onClick={older}>Load older messages</button>}
            {!messages.length && !loading ? <div className="chat-empty">
              <span aria-hidden="true">✳</span><h3>{archived ? "Archived conversation" : "What are we working on?"}</h3>
              <p>{archived ? "Restore this chat to continue the conversation." : "Bring an idea, a question, or a task. Let's make progress."}</p>
              {!archived && <div className="chat-suggestions">{["Help me plan my week", "What do you remember about me?", "Help me break down a goal"].map((prompt) => <button key={prompt} disabled={disabled || !selected} onClick={() => editDraft(prompt)}>{prompt}</button>)}</div>}
            </div> : messages.map((message, index) => (
              <article className={`message ${message.role === "user" ? "user-message" : "assistant-message"}`} key={txt(message.id) || index}>
                <span className="message-avatar">{message.role === "user" ? "Y" : "✳"}</span>
                <div className="message-body"><div className="message-author">{message.role === "user" ? "You" : "Assistant"}</div><div className="message-bubble"><p>{txt(message.content ?? message.text)}</p></div>
                  <button className="message-copy" type="button" aria-label={`Copy ${message.role === "user" ? "your" : "assistant"} message ${index + 1}`} disabled={disabled} onClick={() => void copyMessage(message, index)}>{copied === (txt(message.id) || String(index)) ? "Copied" : "Copy"}</button>
                </div>
              </article>
            ))}
            {preparingReply && <div className="thinking" role="status"><span aria-hidden="true">✳</span> Working on it.</div>}
            {turn?.error ? <p className="task-error" role="status">{txt(turn.error)}</p> : null}
            <div ref={end} />
          </div>
        </div>
        <div className="composer-dock">
          <form className="composer" onSubmit={(event) => { event.preventDefault(); send(); }}>
            <label htmlFor="message" className="visually-hidden">Message your assistant</label>
            <textarea id="message" value={input} onChange={(event) => editDraft(event.target.value)} disabled={disabled || !selected || archived} rows={2} maxLength={12000} placeholder={archived ? "Restore this chat to continue." : "Ask anything, or describe a task…"} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); } }} />
            <div className="composer-footer"><span>{model}<span className="composer-shortcut"> · Shift + Enter for a new line</span></span>{input && <Button label="Discard draft" variant="secondary" isDisabled={disabled} onClick={() => editDraft()} />}{preparingReply
              ? <Button label="Cancel reply" variant="secondary" isDisabled={disabled} onClick={cancelReply} />
              : <Button label="Send message" variant="primary" isDisabled={disabled || !input.trim() || !selected || archived} onClick={send} />}</div>
          </form>
          {context && <details className="chat-context"><summary>Last request context: {Number(context.characters).toLocaleString()} / {Number(context.limit).toLocaleString()} characters</summary>
            <p>Context preparation model calls: {Number(context.modelCalls)}. Character counts are not token counts.</p>
            {list(context, "sources").length ? <ul>{list(context, "sources").map((source, index) => <li key={`${txt(source.kind)}-${txt(source.id)}-${index}`}>{txt(source.title)} <span>({txt(source.kind)} / {txt(source.id)})</span></li>)}</ul> : <p>No saved sources included.</p>}
          </details>}
          <div className="privacy-note">Review saved context in Memory. Your conversations stay in this workspace.</div>
        </div>
      </section>
    </div>
  );
}
