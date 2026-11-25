import { useState } from "react";
import type { Api, Item } from "../../api/client";
import { Button } from "../../components/controls";
import { Card, Feedback, Status } from "../../components/ui";
import { useAction } from "../../hooks/useAction";
import { obj, time, txt } from "../../lib/format";

export function Readiness({ api }: { api: Api }) {
  const [report, setReport] = useState<Item | null>(null);
  const action = useAction();
  const check = (name: string) => void action.run(async () => {
    setReport(await api("/readiness", { check: name }));
  });
  const download = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "workspace-readiness.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <Card title="Setup diagnostics">
    <p>Check local storage and browser setup, or explicitly verify Google access or the configured model.</p>
    <div className="button-row">
      <Button label="Check local runtime" onClick={() => check("local")} isDisabled={action.busy} />
      <Button label="Check Google read access" onClick={() => check("google")} isDisabled={action.busy} />
      <Button label="Check model (uses model tokens)" onClick={() => check("model")} isDisabled={action.busy} />
    </div>
    <Feedback loading={action.busy} error={action.error} />
    {report && <section aria-label="Readiness report">
      <p>Checked {time(report.checkedAt)}</p>
      {Object.entries(obj(report.checks)).map(([name, result]) => <div key={name} className="record">
        <div className="record-body"><strong>{name}</strong><p>{txt(obj(result).detail)}</p></div>
        <Status value={obj(result).status} />
      </div>)}
      <Button label="Download readiness report" onClick={download} />
    </section>}
  </Card>;
}
