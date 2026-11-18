import { useId, type MouseEventHandler } from "react";

/** Shared native controls keep every tool consistent with the chat workspace. */
export function Button({ label, variant = "secondary", isDisabled, onClick }: {
  label: string; variant?: "primary" | "secondary" | "ghost";
  isDisabled?: boolean; onClick?: MouseEventHandler<HTMLButtonElement>;
}) {
  return <button type="button" className={`workspace-button workspace-button-${variant}`} disabled={isDisabled} onClick={onClick}>{label}</button>;
}

export function TextInput({ label, value, onChange, placeholder, type = "text", isDisabled, "aria-label": ariaLabel }: {
  label: string; value: string; onChange: (value: string) => void;
  placeholder?: string; type?: string; isDisabled?: boolean; "aria-label"?: string;
}) {
  const id = useId();
  return <label className="native-field" htmlFor={id}>{label}<input id={id} aria-label={ariaLabel} type={type} value={value} onChange={event => onChange(event.target.value)} placeholder={placeholder} disabled={isDisabled}/></label>;
}
