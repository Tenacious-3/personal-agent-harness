import { type Api } from "../../api/client";
import { txt, pretty, list, time, obj } from "../../lib/format";
import { useResource } from "../../hooks/useResource";
import { Empty, Card, Feedback, Refresh } from "../../components/ui";
import { Button } from "../../components/controls";
import type { Navigate } from "../../app/navigation";

export function Activity({ api, navigate }: { api: Api; navigate: Navigate }) {
  const resource = useResource(api, "/events", true);
  const items = list(resource.data, "events");
  return (
    <>
      <Feedback loading={resource.loading} error={resource.error} />
      <Card title="Activity log" actions={<Refresh reload={resource.reload} />}>
        <p className="section-description">
          Recorded execution and decisions. A request or approval is not proof
          that an action completed.
        </p>
      </Card>
      <section className="record-list">
        {!items.length && !resource.loading && (
          <Empty title="Nothing recorded yet.">
            Execution and approval events appear here.
          </Empty>
        )}
        {items.map((item, index) => (
          <article className="record" key={txt(item.id) || index}>
            <div className="record-body">
              <div className="task-heading">
                <h3>{txt(item.type ?? item.kind)}</h3>
                <span className="record-meta">
                  {time(item.createdAt ?? item.created_at)}
                </span>
              </div>
              {Boolean(item.taskId ?? item.task_id) && (
                <Button label="View task" onClick={() => navigate("Tasks", txt(item.taskId ?? item.task_id))} />
              )}
              {Boolean(obj(item.detail).approvalId) &&
                <Button label="View approval" onClick={() => navigate("Approvals", txt(obj(item.detail).approvalId))} />}
              {txt(item.type ?? item.kind) === "run.usage" && (
                <p className="section-description">
                  {txt(obj(item.detail ?? item.data ?? item.payload).inputTokens)} input tokens ·{" "}
                  {txt(obj(item.detail ?? item.data ?? item.payload).cachedInputTokens)} cached input tokens ·{" "}
                  {txt(obj(item.detail ?? item.data ?? item.payload).outputTokens)} output tokens ·{" "}
                  {txt(obj(item.detail ?? item.data ?? item.payload).contextCharacters)} context characters
                </p>
              )}
              <details>
                <summary>Event details</summary>
                <pre>{pretty(item.detail ?? item.data ?? item.payload)}</pre>
              </details>
            </div>
          </article>
        ))}
      </section>
    </>
  );
}
