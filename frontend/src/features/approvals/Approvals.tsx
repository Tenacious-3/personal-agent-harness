import { useState } from "react";
import { Button } from "../../components/controls";
import { type Api, type Item } from "../../api/client";
import { txt, pretty, list, time } from "../../lib/format";
import { useResource } from "../../hooks/useResource";
import { useAction } from "../../hooks/useAction";
import type { Navigate } from "../../app/navigation";
import {
  Empty,
  Card,
  Status,
  Feedback,
  Area,
  Refresh,
} from "../../components/ui";

function ReconcileAction({
  item,
  api,
  reload,
}: {
  item: Item;
  api: Api;
  reload: () => Promise<void>;
}) {
  const [evidence, setEvidence] = useState("");
  const [outcome, setOutcome] = useState("completed");
  const action = useAction(reload);
  return (
    <div className="review-form">
      <p className="task-error">
        Check the destination before repeating this action. Recording an outcome
        does not execute it again.
      </p>
      <label className="native-field">
        Outcome you checked
        <select
          value={outcome}
          onChange={(event) => setOutcome(event.target.value)}
        >
          <option value="completed">It completed</option>
          <option value="failed">It did not complete</option>
        </select>
      </label>
      <Area
        label="Evidence from your check"
        value={evidence}
        onChange={setEvidence}
        rows={2}
        maxLength={4000}
      />
      <Feedback error={action.error} />
      <Button
        label="Record checked outcome"
        variant="secondary"
        isDisabled={action.busy || !evidence.trim()}
        onClick={() =>
          void action.run(() =>
            api(`/approvals/${encodeURIComponent(txt(item.id))}/reconcile`, {
              outcome,
              evidence: evidence.trim(),
            }),
          )
        }
      />
    </div>
  );
}

export function Approvals({ api, recordId, recordKind, navigate }: { api: Api; recordId?: string; recordKind?: string; navigate: Navigate }) {
  const exactApproval = recordId && recordKind !== "task";
  const resource = useResource(api, recordId
    ? exactApproval ? `/approvals/${encodeURIComponent(recordId)}` : `/approvals?taskId=${encodeURIComponent(recordId)}`
    : "/approvals", true);
  const action = useAction(resource.reload);
  const items = exactApproval ? resource.data.id === recordId ? [resource.data] : [] : list(resource.data, "approvals");
  return (
    <>
      {recordId && <Button label="All approvals" onClick={() => navigate("Approvals")} />}
      <Feedback
        loading={resource.loading}
        error={resource.error || action.error}
      />
      <Card
        title="Exact actions, explicit decisions"
        actions={<Refresh reload={resource.reload} />}
      >
        <p className="section-description">
          Approval is tied to the payload fingerprint below. Results report
          execution separately from your decision.
        </p>
      </Card>
      <section className="record-list">
        {!items.length && !resource.loading && (
          <Empty title="Nothing waiting on you.">
            Proposed external actions appear here before execution.
          </Empty>
        )}
        {items.map((item) => (
          <article className="record" key={txt(item.id)}>
            <div className="record-body">
              <div className="task-heading">
                <h3>{txt(item.summary) || txt(item.kind)}</h3>
                <Status value={item.status} />
              </div>
              <div className="record-meta">
                <span>{txt(item.kind)}</span>
                <span>Expires: {time(item.expiresAt)}</span>
              </div>
              <pre>{pretty(item.payload)}</pre>
              <div className="button-row">
                {!exactApproval && <Button label="Inspect approval" onClick={() => navigate("Approvals", txt(item.id))} />}
                {Boolean(item.taskId) && <Button label="View task" onClick={() => navigate("Tasks", txt(item.taskId))} />}
              </div>
              <details>
                <summary>Action fingerprint</summary>
                <code className="break-text">{txt(item.digest)}</code>
              </details>
              {["pending", "pending_approval"].includes(txt(item.status)) && (
                <div className="button-row spaced">
                  <Button
                    label="Approve this exact action"
                    variant="primary"
                    isDisabled={action.busy}
                    onClick={() =>
                      void action.run(() =>
                        api(
                          `/approvals/${encodeURIComponent(txt(item.id))}/decision`,
                          { decision: "approved", expectedDigest: item.digest },
                        ),
                      )
                    }
                  />
                  <Button
                    label="Reject"
                    variant="secondary"
                    isDisabled={action.busy}
                    onClick={() =>
                      void action.run(() =>
                        api(
                          `/approvals/${encodeURIComponent(txt(item.id))}/decision`,
                          { decision: "rejected", expectedDigest: item.digest },
                        ),
                      )
                    }
                  />
                </div>
              )}
              {Boolean(item.result) && (
                <details>
                  <summary>Execution result</summary>
                  <pre>{pretty(item.result)}</pre>
                </details>
              )}
              {Boolean(item.error) && (
                <p className="task-error">{txt(item.error)}</p>
              )}
              {item.status === "uncertain" && (
                <ReconcileAction
                  item={item}
                  api={api}
                  reload={resource.reload}
                />
              )}
            </div>
          </article>
        ))}
      </section>
    </>
  );
}
