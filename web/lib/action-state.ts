export type DecisionState = {message: string};
export type DecisionAction = (state: DecisionState, form: FormData) => Promise<DecisionState>;
