'use server';
import {revalidatePath} from 'next/cache';
import {redirect} from 'next/navigation';
import {ApiError, postDecision} from '@/lib/api';
import type {DecisionState} from '@/lib/action-state';

export async function submitDecision(findingId: string, _previous: DecisionState, form: FormData): Promise<DecisionState> {
  const rawNote = form.get('note');
  const decision = form.get('decision');
  const note = typeof rawNote === 'string' ? rawNote.trim() : '';
  if (!/^[1-9]\d*$/.test(findingId)) return {message: 'Invalid finding identifier.'};
  if (decision !== 'approve' && decision !== 'reject') return {message: 'Choose Approve or Reject.'};
  if (!note || note.length > 1000) return {message: 'Enter a note of 1–1000 characters.'};
  try {await postDecision(findingId, decision, note);} catch (error) {
    return {message: error instanceof ApiError ? error.message : 'Review API response does not match the decision contract. Please try again.'};
  }
  revalidatePath('/', 'layout');
  redirect(`/findings/${findingId}`);
}
