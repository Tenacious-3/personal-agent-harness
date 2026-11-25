import { Button } from "../../components/controls";
import { TextInput } from "../../components/controls";
import { type Api } from "../../api/client";
import { txt, obj, list } from "../../lib/format";
import { useResource } from "../../hooks/useResource";
import { useAction } from "../../hooks/useAction";
import { useDraft } from "../../lib/drafts";
import {
  Empty,
  Card,
  Status,
  Feedback,
  Area,
  Refresh,
} from "../../components/ui";

export function Goals({ api }: { api: Api }) {
  const resource = useResource(api, "/goals");
  const action = useAction(resource.reload);
  const titleDraft = useDraft("goals:title");
  const descriptionDraft = useDraft("goals:description");
  const stepsDraft = useDraft("goals:steps");
  const title = titleDraft.value ?? "";
  const description = descriptionDraft.value ?? "";
  const steps = stepsDraft.value ?? "";
  const discard = () => { titleDraft.write(); descriptionDraft.write(); stepsDraft.write(); };
  const items = list(resource.data, "goals");
  const save = () =>
    void action.run(async () => {
      await api("/goals", {
        title: title.trim(),
        description: description.trim(),
        steps: steps
          .split("\n")
          .map((step) => step.trim())
          .filter(Boolean),
      });
      discard();
    });
  return (
    <>
      <Feedback
        loading={resource.loading}
        error={resource.error || action.error}
        notice={action.notice || titleDraft.warning}
      />

      <section className="record-list" aria-label="Your goals">
        {items.length > 0 && <h2 className="workspace-list-heading">Tracking <span>{items.length}</span></h2>}
        {!items.length && !resource.loading && (
          <Empty title="A clear direction starts here.">
            Create a goal, then ask your assistant to advance it.
          </Empty>
        )}
        {items.map((item) => (
          <article className="record" key={txt(item.id)}>
            <div className="record-body">
              <div className="task-heading">
                <h3>{txt(item.title)}</h3>
                <Status value={item.status} />
              </div>
              <p>{txt(item.description)}</p>
              {Array.isArray(item.steps) && (
                <ol className="goal-steps">
                  {item.steps.map((step, index) => (
                    <li key={index}>
                      {typeof step === "string"
                        ? step
                        : txt(obj(step).title ?? obj(step).text)}
                      {obj(step).status === "completed" ? " (done)" : ""}
                    </li>
                  ))}
                </ol>
              )}
              <div className="button-row">
                <Button
                  label="Advance goal"
                  variant="primary"
                  isDisabled={action.busy || item.status === "completed"}
                  onClick={() =>
                    void action.run(
                      () =>
                        api(
                          `/goals/${encodeURIComponent(txt(item.id))}/run`,
                          {},
                        ),
                      "Task started. Follow its progress in Tasks.",
                    )
                  }
                />
                <Button
                  label="Mark complete"
                  variant="secondary"
                  isDisabled={action.busy || item.status === "completed"}
                  onClick={() =>
                    void action.run(() =>
                      api(
                        `/goals/${encodeURIComponent(txt(item.id))}`,
                        { status: "completed" },
                        "PATCH",
                      ),
                    )
                  }
                />
              </div>
            </div>
          </article>
        ))}
      </section>
      <Card
        title="Choose a direction"
        actions={<Refresh reload={resource.reload} />}
      >
        <form
          className="stack-form"
          onSubmit={(event) => {
            event.preventDefault();
            save();
          }}
        >
          <TextInput
            label="Goal"
            value={title}
            onChange={(value) => titleDraft.write(value || undefined)}
            isDisabled={action.busy}
            placeholder="Prepare for my next interview"
          />
          <Area
            label="What success looks like"
            value={description}
            onChange={(value) => descriptionDraft.write(value || undefined)}
            disabled={action.busy}
            rows={2}
          />
          <Area
            label="Starting steps (one per line, optional)"
            value={steps}
            onChange={(value) => stepsDraft.write(value || undefined)}
            disabled={action.busy}
            rows={3}
          />
          <Button
            label="Create goal"
            variant="primary"
            isDisabled={action.busy || !title.trim()}
            onClick={save}
          />
          {(title || description || steps) && <Button label="Discard goal draft" isDisabled={action.busy} onClick={discard} />}
        </form>
      </Card>
    </>
  );
}
