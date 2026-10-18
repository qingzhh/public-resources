const contextKey = context => context?.ready ? JSON.stringify([context.sessionId, context.modelKey, context.cwd]) : '';

export class FastController {
  constructor(dispatch) {
    this.dispatch = dispatch;
    this.state = { context: null, data: null, pending: false, error: '', message: '' };
    this.listeners = new Set();
    this.sequence = 0;
    this.disposed = false;
    this.reading = false;
    this.feedbackTimer = undefined;
    this.subscribe = listener => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
    this.snapshot = () => this.state;
  }
  publish(patch) {
    if (this.disposed) return;
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener();
  }
  clearFeedback() { clearTimeout(this.feedbackTimer); this.feedbackTimer = undefined; }
  fail(error) {
    this.clearFeedback();
    this.publish({ data: null, pending: false, error: error instanceof Error ? error.message : String(error), message: '' });
  }
  setContext(context) {
    if (contextKey(context) === contextKey(this.state.context) && context?.reason === this.state.context?.reason) return;
    this.sequence += 1;
    this.reading = false;
    this.clearFeedback();
    this.publish({ context, data: null, pending: false, error: '', message: '' });
    if (context.ready) return this.refresh();
  }
  validateReply(data, context) {
    if (data?.sessionId !== context.sessionId || data?.modelKey !== context.modelKey || typeof data?.active !== 'boolean' || typeof data?.desiredActive !== 'boolean' || typeof data?.supported !== 'boolean' || data.active !== (data.desiredActive && data.supported)) throw new Error('Fast 状态响应与当前会话不匹配');
    return data;
  }
  async refresh() {
    const context = this.state.context;
    if (this.disposed || !context?.ready || this.state.pending || this.reading) return;
    const sequence = this.sequence;
    this.reading = true;
    try {
      const reply = await this.dispatch('plugin.call', { method: 'fast.read', args: { context } });
      if (!this.disposed && sequence === this.sequence) {
        const data = this.validateReply(reply, context);
        const changed = data.active !== this.state.data?.active || data.desiredActive !== this.state.data?.desiredActive;
        if (changed) this.clearFeedback();
        this.publish({ data, error: '', message: changed ? '' : this.state.message });
      }
    } catch (error) { if (sequence === this.sequence) this.fail(error); }
    finally { if (sequence === this.sequence) this.reading = false; }
  }
  async toggle() {
    const { context, data, pending } = this.state;
    if (this.disposed || !context?.ready || !data?.supported || pending) return;
    const sequence = ++this.sequence;
    this.reading = false;
    this.clearFeedback();
    this.publish({ pending: true, error: '', message: '' });
    try {
      const reply = await this.dispatch('plugin.call', { method: 'fast.set', args: { context, desiredActive: !data.desiredActive } });
      if (this.disposed || sequence !== this.sequence) return;
      const next = this.validateReply(reply, context);
      this.publish({ data: next, pending: false, message: next.active ? 'Fast 已开启，后续请求将申请 priority。' : 'Fast 已关闭，后续请求使用普通模式。' });
      this.feedbackTimer = setTimeout(() => this.publish({ message: '' }), 3000);
      this.feedbackTimer.unref?.();
    } catch (error) { if (sequence === this.sequence) this.fail(error); }
  }
  dispose() { this.disposed = true; this.sequence += 1; this.clearFeedback(); this.listeners.clear(); }
}
