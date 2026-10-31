import {notFound} from 'next/navigation';
import {ApiError, getFinding} from '@/lib/api';
import {FindingView} from '@/components/finding-view';
import {ReviewError, safeMessage} from '@/components/common';
import {submitDecision} from '@/app/actions';
export default async function Finding({params}: {params: Promise<{findingId: string}>}) {
  const {findingId} = await params;
  if (!/^[1-9]\d*$/.test(findingId)) notFound();
  let detail;
  try {detail = await getFinding(findingId);} catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    return <ReviewError message={safeMessage(error)} />;
  }
  return <FindingView detail={detail} action={submitDecision.bind(null, findingId)} />;
}
