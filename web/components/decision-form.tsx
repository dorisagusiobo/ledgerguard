'use client';
import {useActionState, useState} from 'react';
import type {DecisionAction} from '@/lib/action-state';

export function DecisionForm({action}: {action: DecisionAction}) {
  const [state, formAction, pending] = useActionState(action, {message: ''});
  const [note, setNote] = useState('');
  const [decision, setDecision] = useState<'approve' | 'reject'>('approve');
  const [validation, setValidation] = useState('');
  return <form action={formAction} className="decision-form" onReset={event => event.preventDefault()} onSubmit={event => {
    if (!note.trim() || note.trim().length > 1000) {
      event.preventDefault(); setValidation('Enter a note of 1–1000 characters.');
    } else setValidation('');
  }}>
    <fieldset disabled={pending}><legend>Choose a decision</legend>
      <div className="decision-options">
        <label><input type="radio" name="decision" value="approve" checked={decision === 'approve'} onChange={() => setDecision('approve')} /> Approve finding</label>
        <label><input type="radio" name="decision" value="reject" checked={decision === 'reject'} onChange={() => setDecision('reject')} /> Reject finding</label>
      </div>
      <label className="field-label" htmlFor="decision-note">Decision note <span>Required</span></label>
      <textarea id="decision-note" name="note" value={note} onChange={event => setNote(event.target.value)}
        required maxLength={1000} rows={5} aria-describedby="note-help decision-message" placeholder="Explain the evidence behind your decision…" />
      <div className="field-help" id="note-help"><span>1–1000 characters. Your decision will be added to the history.</span><span>{note.length}/1000</span></div>
      <p id="decision-message" role={validation || state.message ? 'alert' : undefined} className="form-message">{validation || state.message}</p>
      <button className="primary-button" type="submit" disabled={pending}>{pending ? 'Saving decision…' : 'Record decision'} <span aria-hidden="true">↗</span></button>
    </fieldset>
    <p className="small muted">Recording a decision does not pay an invoice or trigger another action.</p>
  </form>;
}
