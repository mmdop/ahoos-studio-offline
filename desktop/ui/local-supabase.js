/* A stand-in for the part of supabase-js the studio uses, backed by this computer.
 *
 * The web studio's app.js talks to Supabase. The offline build runs that same
 * file unchanged, and this is what it finds at window.supabase instead: the same
 * call shapes, answered by the desktop app's own store (desktop/store.py).
 *
 * Only what app.js calls is here -- auth that is always signed in as the one
 * local person, a query builder with select/insert/update/delete/eq/order/
 * limit/single, and storage upload/download. Anything else throws with its name,
 * because a stand-in that quietly ignores a filter returns every row and looks
 * like it worked.
 *
 * THE BUILDER IS A THENABLE
 *
 * supabase-js queries are awaited directly -- `await sb.from('chats').select()
 * .order(...)` -- with no .execute(). So the builder records each call and runs
 * the request only when something awaits it, which is when `then` is called.
 */
(function () {
  'use strict';

  const LOCAL_USER = {
    id: '00000000-0000-0000-0000-000000000001',
    email: '',
    user_metadata: { name: '' },
  };
  const SESSION = { user: LOCAL_USER, access_token: 'local' };

  async function post(url, body) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      return { data: null, error: { message: `the local store answered ${response.status}` } };
    }
    return response.json();
  }

  class Query {
    constructor(table) {
      this.table = table;
      this.spec = { op: 'select', columns: '*', filters: [], order: [] };
    }

    select(columns) {
      // After insert/update it means "return the rows", which the store always
      // does; before, it names the columns.
      if (this.spec.op === 'select') this.spec.columns = columns || '*';
      else if (columns) this.spec.columns = columns;
      return this;
    }
    insert(values) { this.spec.op = 'insert'; this.spec.values = values; return this; }
    update(values) { this.spec.op = 'update'; this.spec.values = values; return this; }
    delete()       { this.spec.op = 'delete'; return this; }
    eq(column, value) { this.spec.filters.push([column, 'eq', value]); return this; }
    order(column, options) {
      this.spec.order.push([column, !(options && options.ascending === false)]);
      return this;
    }
    limit(count)  { this.spec.limit = count; return this; }
    single()      { this.spec.single = true; return this; }

    then(resolve, reject) {
      return post('/local/db/' + encodeURIComponent(this.table), this.spec).then(resolve, reject);
    }
  }

  // Everything supabase-js has that app.js does not use fails by name.
  const unsupported = name => () => { throw new Error('local store: ' + name + ' is not implemented'); };
  for (const name of ['upsert', 'neq', 'gt', 'gte', 'lt', 'lte', 'like', 'ilike', 'in', 'is',
                      'or', 'not', 'range', 'match', 'maybeSingle', 'rpc']) {
    Query.prototype[name] = unsupported(name);
  }

  const storage = {
    from(bucket) {
      const base = '/local/storage/' + encodeURIComponent(bucket) + '/';
      return {
        async upload(path, blob) {
          const response = await fetch(base + path, { method: 'PUT', body: blob });
          return response.ok ? { data: { path }, error: null }
                             : { data: null, error: { message: 'could not save ' + path } };
        },
        async download(path) {
          const response = await fetch(base + path);
          return response.ok ? { data: await response.blob(), error: null }
                             : { data: null, error: { message: 'no file at ' + path } };
        },
      };
    },
  };

  const auth = {
    async getSession() { return { data: { session: SESSION }, error: null }; },
    onAuthStateChange(callback) {
      // Signed in from the first moment, and never otherwise.
      setTimeout(() => callback('SIGNED_IN', SESSION), 0);
      return { data: { subscription: { unsubscribe() {} } } };
    },
    async signOut() { return { error: null }; },
    async signInWithOAuth() { return { data: null, error: null }; },
  };

  window.supabase = {
    createClient() {
      return { auth, storage, from: table => new Query(table) };
    },
  };
})();
