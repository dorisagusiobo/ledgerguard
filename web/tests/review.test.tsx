import {describe, it, expect, vi} from 'vitest';
import {render, screen, fireEvent, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {FindingView} from '@/components/finding-view';
import {DecisionForm} from '@/components/decision-form';
import {detail} from './fixture';
describe('evidence review', () => {
  it('renders vendor markup as literal text without executable elements', () => {
    const {container} = render(<FindingView detail={detail} action={async () => ({message: ''})} />);
    expect(screen.getByText(detail.finding.notes[0].summary).textContent).toBe(detail.finding.notes[0].summary);
    expect(screen.getByText(detail.invoices[0].line_items[0].description).textContent).toBe(detail.invoices[0].line_items[0].description);
    expect(container.querySelectorAll('script,img').length).toBe(0);
    expect(screen.getByText('AI-generated note')).toBeTruthy();
  });
  it('shows amounts unchanged and highlights matching source text', () => {
    const {container} = render(<FindingView detail={detail} action={async () => ({message: ''})} />);
    expect(screen.getByText('900719925474.09', {selector: '.large-amount'}).textContent).toBe('900719925474.09');
    expect(screen.getByText('12.3456', {selector: 'td'}).textContent).toBe('12.3456');
    expect(screen.getAllByText('37.04').every(element => element.textContent === '37.04')).toBe(true);
    expect(container.querySelectorAll('tr.evidence-match').length).toBe(1);
    expect(screen.getByText('Page 2', {selector: '.page-label'})).toBeTruthy();
  });
  it('shows the specified no-clause text', () => {
    render(<FindingView detail={{...detail, finding: {...detail.finding, citation: null, no_clause_reason: 'no applicable clause identified'}}} action={async () => ({message: ''})} />);
    expect(screen.getByText('No applicable clause identified')).toBeTruthy();
  });
});
describe('decision form', () => {
  it('does not submit an empty or whitespace-only note', async () => {
    const action = vi.fn(async () => ({message: ''}));
    const {container} = render(<DecisionForm action={action} />);
    fireEvent.submit(container.querySelector('form')!);
    expect(action).not.toHaveBeenCalled();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/Decision note/), '   ');
    await user.click(screen.getByRole('button', {name: /Record decision/}));
    expect(action).not.toHaveBeenCalled();
    expect(screen.getByRole('alert').textContent).toContain('1–1000');
  });
  it('keeps the draft when service is unavailable', async () => {
    const action = vi.fn(async () => ({message: 'Service unavailable'}));
    render(<DecisionForm action={action} />);
    const user = userEvent.setup();
    const note = screen.getByLabelText(/Decision note/) as HTMLTextAreaElement;
    await user.type(note, 'Synthetic evidence supports rejecting the finding.');
    await user.click(screen.getByRole('radio', {name: 'Reject finding'}));
    await user.click(screen.getByRole('button', {name: /Record decision/}));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('Service unavailable'));
    expect(note.value).toBe('Synthetic evidence supports rejecting the finding.');
    expect((screen.getByRole('radio', {name: 'Reject finding'}) as HTMLInputElement).checked).toBe(true);
    expect(action).toHaveBeenCalledTimes(1);
  });
});
