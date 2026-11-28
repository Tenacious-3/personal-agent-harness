import { useEffect, useState } from "react";
import type { Api, Item } from "../../api/client";
import { Button } from "../../components/controls";
import { Card, Feedback } from "../../components/ui";
import { useAction } from "../../hooks/useAction";
import { txt } from "../../lib/format";

export function ManualBrowser({ api, lease, onEnd }: { api: Api; lease: string; onEnd: () => Promise<void> }) {
  const [frame, setFrame] = useState<Item | null>(null);
  const [entry, setEntry] = useState("");
  const action = useAction();
  const readFrame = async () => setFrame(await api("/browser/manual/frame", { lease }));
  const input = (kind: string, values: Item) => {
    if (action.busy || !frame) return;
    void action.run(async () => {
      await api("/browser/manual/input", { lease, frame: frame.frame, kind, ...values });
      setFrame(null);
      await readFrame();
    });
  };
  useEffect(() => {
    void action.run(readFrame);
  }, [lease]);

  return <Card title="Dedicated browser sign-in">
    <p>The assistant is paused. Select a field in the browser view, enter its value below, and use Enter or click the page to submit. The session expires after 10 minutes.</p>
    <Feedback error={action.error} loading={action.busy} />
    {frame && <>
      <p>Current site <strong>{txt(frame.origin)}</strong></p>
      {Boolean(frame.title) && <p>{txt(frame.title)}</p>}
      <img
        className="browser-screenshot manual-browser-view"
        alt="Interactive view of the current dedicated browser tab"
        src={`data:image/jpeg;base64,${txt(frame.image)}`}
        draggable={false}
        onClick={(event) => {
          const box = event.currentTarget.getBoundingClientRect();
          input("click", {
            x: Math.floor((event.clientX - box.left) * Number(frame.width) / box.width),
            y: Math.floor((event.clientY - box.top) * Number(frame.height) / box.height),
          });
        }}
      />
    </>}
    <div className="stack-form">
      <label className="native-field">
        Text for the focused browser field
        <input type="password" autoComplete="off" value={entry} maxLength={4096} onChange={(event) => setEntry(event.target.value)} />
      </label>
      <div className="button-row">
        <Button label="Type into browser" isDisabled={!entry || !frame || action.busy} onClick={() => {
          input("text", { value: entry });
          setEntry("");
        }} />
        {["Tab", "Shift+Tab", "Backspace", "Enter", "Escape", "PageUp", "PageDown"].map((key) => (
          <Button key={key} label={key} isDisabled={!frame || action.busy} onClick={() => input("key", { value: key })} />
        ))}
        <Button label="Refresh browser view" isDisabled={action.busy} onClick={() => void action.run(readFrame)} />
        <Button label="End sign-in and return control" isDisabled={action.busy} onClick={() => void action.run(async () => {
          await api("/browser/manual/end", { lease });
          await onEnd();
        })} />
      </div>
    </div>
  </Card>;
}
