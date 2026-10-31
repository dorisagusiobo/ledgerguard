'use client';
export default function ErrorPage({reset}: {reset: () => void}) {return <div className="empty-state" role="alert"><h1>This review could not be loaded.</h1><p>Please try again. Your previously saved decisions remain recorded.</p><button className="primary-button" onClick={reset}>Try again</button></div>;}
