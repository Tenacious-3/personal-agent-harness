import { useState } from "react";
import { Button } from "../../components/controls";
import { TextInput } from "../../components/controls";
import { type Api } from "../../api/client";
import { txt, list, obj, time } from "../../lib/format";
import { useResource } from "../../hooks/useResource";
import { useAction } from "../../hooks/useAction";
import { Empty, Card, Status, Feedback, Area } from "../../components/ui";
import { type Navigate } from "../../app/navigation";

export function Tasks({
  api,
  navigate,
  selected,
}: {
  api: Api;
  navigate: Navigate;
  selected?: string;
}) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [pages, setPages] = useState<string[]>([]);
  const params = new URLSearchParams({ q: query, status, limit: "25" });
  if (pages.length) params.set("before", pages[pages.length - 1]);
  const resource = useResource(api, selected ? `/tasks/${encodeURIComponent(selected)}` : `/tasks?${params}`, true);
  const action = useAction(resource.reload);
  const [prompt, setPrompt] = useState("");
  const [editing, setEditing] = useState("");
  const [memory, setMemory] = useState("");
  const items = selected ? (resource.data.id === selected ? [resource.data] : []) : list(resource.data, "tasks");
  const counts = obj(resource.data.counts);
  return (
    <>
      <Feedback
        loading={resource.loading}
        error={resource.error || action.error}
      />
      <Card
        title="Keep the work moving"
        actions={
          <Button
            label="Run a dream"
            variant="secondary"
            isDisabled={action.busy}
            onClick={() =>
              void action.run(() =>
                api("/tasks", {
                  kind: "dream",
                  prompt:
                    "Consolidate recent activity into evidence-backed memories.",
                }),
              )
            }
          />
        }
      >
        <div className="inline-form">
          <TextInput
            label="Background task"
            value={prompt}
            onChange={setPrompt}
            placeholder="Review my saved context and suggest priorities"
          />
          <Button
            label="Start task"
            variant="primary"
            isDisabled={action.busy || !prompt.trim()}
            onClick={() =>
              void action.run(async () => {
                await api("/tasks", { kind: "task", prompt: prompt.trim() });
                setPrompt("");
              })
            }
          />
        </div>
      </Card>
      <Card title="Task history">
        {selected ? <Button label="Back to task history" onClick={() => navigate("Tasks")} /> : <>
          <div className="inline-form">
            <TextInput label="Search tasks" value={query} onChange={(value) => { setQuery(value.slice(0, 200)); setPages([]); }} />
            <label className="native-field">Status
              <select aria-label="Task status" value={status} onChange={(event) => { setStatus(event.target.value); setPages([]); }}>
                <option value="">All statuses</option>
                {['queued', 'running', 'waiting_approval', 'completed', 'failed', 'cancelled'].map((value) =>
                  <option key={value} value={value}>{value.replaceAll('_', ' ')}</option>)}
              </select>
            </label>
          </div>
          {resource.data.counts && <p>{Number(counts.queued) || 0} queued · {Number(counts.running) || 0} running · {Number(counts.waiting_approval) || 0} waiting for approval</p>}
        </>}
      </Card>
      <section className="record-list" aria-label="Retained tasks">
        {!items.length && !resource.loading && (
          <Empty title="A clear slate.">
            Background tasks and dream results appear here.
          </Empty>
        )}
        {items.map((item) => (
          <article className="record" key={txt(item.id)}>
            <div className="record-body">
              <div className="task-heading">
                <p>{txt(item.prompt)}</p>
                <Status value={item.status} />
              </div>
              <div className="record-meta">
                <span>{txt(item.kind)}</span>
                <span>{time(item.createdAt ?? item.created_at)}</span>
                {Boolean(item.goalId) && <span>Goal: {txt(item.goalId)}</span>}
                {Boolean(item.parentId) && <span>Parent: {txt(item.parentId)}</span>}
                {selected && <span>Task ID: {selected}</span>}
              </div>
              {Boolean(item.result) && (
                <details>
                  <summary>View result</summary>
                  <pre>{txt(item.result)}</pre>
                </details>
              )}
              {Boolean(item.error) && (
                <p className="task-error">{txt(item.error)}</p>
              )}
              {item.status === "waiting_approval" && (
                <Button
                  label="Review pending actions"
                  variant="secondary"
                  onClick={() => navigate("Approvals", txt(item.id), "task")}
                />
              )}
              <div className="button-row">
                {!selected && <Button label="Inspect task" onClick={() => navigate("Tasks", txt(item.id))} />}
                {['queued', 'running', 'waiting_approval', 'failed'].includes(txt(item.status)) && <Button
                  label="Cancel task" isDisabled={action.busy}
                  onClick={() => void action.run(() => api(`/tasks/${encodeURIComponent(txt(item.id))}/cancel`, {}))}
                />}
                {item.status === 'failed' && <Button
                  label="Retry task (may use model tokens)" isDisabled={action.busy || item.requiresReconciliation === true}
                  onClick={() => void action.run(() => api(`/tasks/${encodeURIComponent(txt(item.id))}/retry`, {}))}
                />}
              </div>
              {item.requiresReconciliation === true && <Button label="Review action outcome before retrying" onClick={() => navigate("Approvals", txt(item.id), "task")} />}
              {Boolean(item.parentId) && selected && <Button label="Open parent task" onClick={() => navigate("Tasks", txt(item.parentId))} />}
              {item.kind === "dream" &&
                item.status === "completed" &&
                Boolean(item.result) && (
                  <div className="dream-review">
                    <Button
                      label="Review memory proposal"
                      variant="secondary"
                      onClick={() => {
                        setEditing(txt(item.id));
                        setMemory(txt(item.result));
                      }}
                    />
                    {editing === txt(item.id) && (
                      <div className="review-form">
                        <Area
                          label="Edit the memory to keep"
                          value={memory}
                          onChange={setMemory}
                          maxLength={2000}
                        />
                        <p>
                          This saves your reviewed text. If memory changed after
                          the dream, generate a fresh proposal.
                        </p>
                        <Button
                          label="Save reviewed memory"
                          variant="primary"
                          isDisabled={action.busy || !memory.trim()}
                          onClick={() =>
                            void action.run(async () => {
                              await api("/dreams/apply", {
                                taskId: txt(item.id),
                                text: memory.trim(),
                              });
                              setEditing("");
                              setMemory("");
                            })
                          }
                        />
                      </div>
                    )}
                  </div>
                )}
            </div>
          </article>
        ))}
      </section>
      {!selected && <div className="button-row">
        {pages.length > 0 && <Button label="Newer tasks" onClick={() => setPages((current) => current.slice(0, -1))} />}
        {Boolean(resource.data.nextBefore) && <Button label="Older tasks" onClick={() => setPages((current) => [...current, txt(resource.data.nextBefore)])} />}
      </div>}
    </>
  );
}
