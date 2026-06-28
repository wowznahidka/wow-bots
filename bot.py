"""
WOW Bots v4.0 — MAXIMUM EDITION
Фічі: Фото · Промокоди · Кошик+/- · Нотатки · Варіанти товарів
       Лояльність · Розсилка · Monobank оплата · Мультиадмін
       in_stock · Повтор замовлення · /stats · /points · /broadcast
Run: BOT_TOKEN=xxx python bot.py
"""
import os, json, logging, asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN', '')
DIR = os.path.dirname(os.path.abspath(__file__))

# ── File helpers ──────────────────────────────────────────────────
def load_f(name, default=None):
    p = os.path.join(DIR, name)
    if not os.path.exists(p): return {} if default is None else default
    with open(p, encoding='utf-8') as f: return json.load(f)

def save_f(name, data):
    with open(os.path.join(DIR, name), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── Config ────────────────────────────────────────────────────────
CFG          = load_f('config.json')
OWNER        = str(CFG.get('owner_chat_id', ''))
EXTRA_ADMINS = [str(x) for x in CFG.get('extra_admins', [])]
ALL_ADMINS   = list(dict.fromkeys([OWNER] + EXTRA_ADMINS))  # unique, owner first
PROMOS       = CFG.get('promo_codes', {})                   # {"CODE": pct}
PAYMENT      = CFG.get('payment', {})                       # {enabled, monobank_jar}
LYL          = CFG.get('loyalty', {})                       # {enabled, points_per_100uah, min_redeem, value_per_point}
LYL_ON       = LYL.get('enabled', False)
LYL_RATE     = LYL.get('points_per_100uah', 1)
LYL_MIN      = LYL.get('min_redeem', 100)
LYL_VAL      = LYL.get('value_per_point', 0.5)             # UAH per point

# ── Per-user runtime ──────────────────────────────────────────────
carts:   dict[int, list] = {}   # uid → [{item, qty, variants}]
states:  dict[int, str]  = {}   # uid → 'promo'|'note'|'broadcast'
pending: dict[int, dict] = {}   # uid → {loyalty_disc, promo_disc, note, _pts_used}
var_ctx: dict[int, dict] = {}   # uid → variant selection context

# ── Users registry ────────────────────────────────────────────────
def reg_user(user):
    db = load_f('users.json', {'users': {}})
    db['users'][str(user.id)] = {
        'name': user.full_name,
        'username': f"@{user.username}" if user.username else '',
        'last': datetime.now().strftime('%Y-%m-%d')
    }
    save_f('users.json', db)

# ── Loyalty ───────────────────────────────────────────────────────
def pts_get(uid):
    return load_f('loyalty.json', {'u': {}}).get('u', {}).get(str(uid), {}).get('pts', 0)

def pts_add(uid, uah):
    earned = int(uah / 100 * LYL_RATE)
    if earned <= 0: return
    db = load_f('loyalty.json', {'u': {}})
    db['u'].setdefault(str(uid), {'pts': 0})['pts'] += earned
    save_f('loyalty.json', db)

def pts_use(uid, pts):
    db = load_f('loyalty.json', {'u': {}})
    u = db['u'].setdefault(str(uid), {'pts': 0})
    u['pts'] = max(0, u['pts'] - pts)
    save_f('loyalty.json', db)

def pts_to_uah(pts): return round(pts * LYL_VAL)

# ── Cart ──────────────────────────────────────────────────────────
def get_cart(uid): return carts.setdefault(uid, [])

def cart_add(uid, item, variants=None):
    v = variants or {}
    for e in get_cart(uid):
        if e['item']['id'] == item['id'] and e.get('variants', {}) == v:
            e['qty'] += 1; return
    get_cart(uid).append({'item': item, 'qty': 1, 'variants': v})

def cart_bump(uid, idx, delta):
    cart = get_cart(uid)
    if 0 <= idx < len(cart):
        cart[idx]['qty'] += delta
        if cart[idx]['qty'] <= 0: cart.pop(idx)

def cart_sub(uid): return sum(e['item']['price'] * e['qty'] for e in get_cart(uid))
def cart_clear(uid): carts[uid] = []

def cart_text(uid):
    cart = get_cart(uid)
    if not cart: return '🛒 Кошик порожній'
    lines = []
    for e in cart:
        vstr = (' (' + ', '.join(e['variants'].values()) + ')') if e.get('variants') else ''
        lines.append(f"• {e['item']['name']}{vstr} ×{e['qty']} — {e['item']['price']*e['qty']} грн")
    return '\n'.join(lines) + f"\n\n💰 *Разом: {cart_sub(uid)} грн*"

# ── Item lookup ───────────────────────────────────────────────────
def find_item(item_id):
    for cat in CFG.get('categories', []):
        for it in cat.get('items', []):
            if it['id'] == item_id: return it, f"cat_{cat['id']}"
        for sub in cat.get('subcategories', []):
            for it in sub.get('items', []):
                if it['id'] == item_id: return it, f"sub_{cat['id']}_{sub['id']}"
    return None, 'back_main'

# ── Orders ────────────────────────────────────────────────────────
def save_order(user, cart, total, ld, pd_pct, pd_uah, note):
    db = load_f('orders.json', {'orders': []})
    db['orders'].append({
        'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'user': {'name': user.full_name, 'id': user.id,
                 'username': f"@{user.username}" if user.username else f"ID:{user.id}"},
        'items': [{'name': e['item']['name'], 'qty': e['qty'],
                   'price': e['item']['price'],
                   'variants': e.get('variants', {})} for e in cart],
        'subtotal': cart_sub(user.id if hasattr(user,'id') else 0),
        'loyalty_disc': ld, 'promo_pct': pd_pct, 'promo_uah': pd_uah,
        'total': total, 'note': note
    })
    save_f('orders.json', db)

# ── Keyboards ─────────────────────────────────────────────────────
def kb_main():
    rows = []
    webapp_url = CFG.get('webapp_url', '')
    if webapp_url:
        rows.append([InlineKeyboardButton('🛍 Відкрити магазин', web_app=WebAppInfo(url=webapp_url))])
    rows += [[InlineKeyboardButton(f"{c.get('emoji','')} {c['name']}", callback_data=f"cat_{c['id']}")] for c in CFG.get('categories', [])]
    r2 = []
    if CFG.get('show_cart_btn', True): r2.append(InlineKeyboardButton('🛒 Кошик', callback_data='cart'))
    if CFG.get('address'):             r2.append(InlineKeyboardButton('📍 Адреса', callback_data='address'))
    if r2: rows.append(r2)
    if CFG.get('faq'):                 rows.append([InlineKeyboardButton('❓ FAQ', callback_data='faq')])
    return InlineKeyboardMarkup(rows)

def _item_btn(it):
    stock = it.get('in_stock', True)
    label = (f"⏳ {it['name']} (немає)" if not stock else f"{it['name']} — {it['price']} грн")
    return InlineKeyboardButton(label, callback_data=f"item_{it['id']}")

def kb_cat(cat_id):
    cat = next((c for c in CFG.get('categories', []) if c['id'] == cat_id), None)
    if not cat: return kb_main()
    rows = []
    if cat.get('subcategories'):
        rows = [[InlineKeyboardButton(f"{s.get('emoji','')} {s['name']}", callback_data=f"sub_{cat_id}_{s['id']}")] for s in cat['subcategories']]
    elif cat.get('items'):
        rows = [[_item_btn(it)] for it in cat['items']]
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data='back_main')])
    return InlineKeyboardMarkup(rows)

def kb_sub(cat_id, sub_id):
    cat = next((c for c in CFG.get('categories', []) if c['id'] == cat_id), None)
    sub = next((s for s in (cat or {}).get('subcategories', []) if s['id'] == sub_id), None)
    if not sub: return kb_cat(cat_id)
    rows = [[_item_btn(it)] for it in sub.get('items', [])]
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data=f"cat_{cat_id}")])
    return InlineKeyboardMarkup(rows)

def kb_item(item_id, back):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🛒 Додати в кошик', callback_data=f"addcart_{item_id}")],
        [InlineKeyboardButton('⬅️ Назад', callback_data=back)],
    ])

def kb_cart(uid):
    cart = get_cart(uid)
    rows = []
    for idx, e in enumerate(cart):
        vstr = (' (' + ', '.join(e['variants'].values()) + ')') if e.get('variants') else ''
        label = (e['item']['name'] + vstr)[:22]
        rows.append([
            InlineKeyboardButton('−', callback_data=f"dec_{idx}"),
            InlineKeyboardButton(f"{label} ×{e['qty']}", callback_data='noop'),
            InlineKeyboardButton('+', callback_data=f"inc_{idx}"),
        ])
    if cart:
        has_last = bool(load_f('last_orders.json', {}).get(str(uid)))
        if has_last:
            rows.append([InlineKeyboardButton('🔄 Повторити попереднє замовлення', callback_data='repeat')])
        rows.append([InlineKeyboardButton('✅ Оформити замовлення', callback_data='checkout')])
        rows.append([InlineKeyboardButton('🗑 Очистити', callback_data='clearcart')])
    rows.append([InlineKeyboardButton('🏠 Головна', callback_data='back_main')])
    return InlineKeyboardMarkup(rows)

def kb_variants(vc):
    key    = vc['keys'][vc['idx']]
    values = vc['item'].get('variants', {}).get(key, [])
    rows   = [[InlineKeyboardButton(v, callback_data=f"vc_{i}")] for i, v in enumerate(values)]
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data=vc['back'])])
    return InlineKeyboardMarkup(rows)

def greet(first=''):
    return CFG.get('greeting', '👋 Привіт, {name}!\n\nОберіть розділ:').replace('{name}', first or '')

# ── Handlers ──────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reg_user(update.effective_user)
    user = update.effective_user
    photo = CFG.get('welcome_photo')
    kw = dict(parse_mode='Markdown', reply_markup=kb_main())
    if photo:
        await update.message.reply_photo(photo=photo, caption=greet(user.first_name), **kw)
    else:
        await update.message.reply_text(greet(user.first_name), **kw)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ALL_ADMINS: return
    orders = load_f('orders.json', {'orders': []}).get('orders', [])
    users  = load_f('users.json', {'users': {}}).get('users', {})
    if not orders:
        await update.message.reply_text('📊 Замовлень поки немає.'); return
    today   = datetime.now().strftime('%Y-%m-%d')
    t_ords  = [o for o in orders if o['time'].startswith(today)]
    counts  = {}
    for o in orders:
        for it in o['items']:
            counts[it['name']] = counts.get(it['name'], 0) + it['qty']
    top     = sorted(counts.items(), key=lambda x: -x[1])[:5]
    top_txt = '\n'.join(f"  {i+1}. {n} — {q} шт" for i, (n, q) in enumerate(top)) or '  —'
    await update.message.reply_text(
        f"📊 *{CFG.get('name','')} — Аналітика*\n\n"
        f"👥 Клієнтів: {len(users)}\n\n"
        f"📅 Сьогодні: {len(t_ords)} зам. / {sum(o['total'] for o in t_ords)} грн\n"
        f"🗓 Всього:    {len(orders)} зам. / {sum(o['total'] for o in orders)} грн\n\n"
        f"🏆 *Топ товарів:*\n{top_txt}",
        parse_mode='Markdown'
    )

async def cmd_points(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not LYL_ON: return
    uid = update.effective_user.id
    pts = pts_get(uid)
    await update.message.reply_text(
        f"🎁 *Ваші бали:*\n\n*{pts} балів* ≈ {pts_to_uah(pts)} грн\n\n"
        f"{LYL_RATE} бал за кожні 100 грн замовлення.\n"
        f"Мінімум для використання: {LYL_MIN} балів.",
        parse_mode='Markdown'
    )

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ALL_ADMINS: return
    states[update.effective_user.id] = 'broadcast'
    await update.message.reply_text(
        "📢 *Розсилка*\n\nВведіть текст — він піде всім клієнтам:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Скасувати', callback_data='cancel_bc')]])
    )

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    d    = q.data
    uid  = q.from_user.id
    chat = q.message.chat_id

    if d == 'noop': return

    if d == 'cancel_bc':
        states.pop(uid, None)
        await q.edit_message_text(greet(q.from_user.first_name), parse_mode='Markdown', reply_markup=kb_main())
        return

    # ── Main ──
    if d == 'back_main':
        var_ctx.pop(uid, None); states.pop(uid, None)
        await q.edit_message_text(greet(q.from_user.first_name), parse_mode='Markdown', reply_markup=kb_main())

    # ── Category ──
    elif d.startswith('cat_'):
        cat_id = d[4:]
        cat = next((c for c in CFG.get('categories', []) if c['id'] == cat_id), None)
        if not cat: return
        await q.edit_message_text(
            f"{cat.get('emoji','')} *{cat['name']}*\n\n{cat.get('desc','Оберіть позицію:')}",
            parse_mode='Markdown', reply_markup=kb_cat(cat_id)
        )

    # ── Subcategory ──
    elif d.startswith('sub_'):
        _, cat_id, sub_id = d.split('_', 2)
        cat = next((c for c in CFG.get('categories', []) if c['id'] == cat_id), None)
        sub = next((s for s in (cat or {}).get('subcategories', []) if s['id'] == sub_id), None)
        if not sub: return
        await q.edit_message_text(
            f"{sub.get('emoji','')} *{sub['name']}*\n\nОберіть товар:",
            parse_mode='Markdown', reply_markup=kb_sub(cat_id, sub_id)
        )

    # ── Item ──
    elif d.startswith('item_'):
        item, back = find_item(d[5:])
        if not item: return
        if not item.get('in_stock', True):
            await q.answer('⏳ Товар тимчасово відсутній', show_alert=True); return
        if item.get('variants'):
            keys = list(item['variants'].keys())
            var_ctx[uid] = {'item_id': d[5:], 'item': item, 'keys': keys, 'idx': 0, 'selected': {}, 'back': back}
            await q.edit_message_text(
                f"*{item['name']}*\n\nОберіть {keys[0]}:",
                parse_mode='Markdown', reply_markup=kb_variants(var_ctx[uid])
            )
            return
        price = "Безкоштовно" if item['price'] == 0 else f"{item['price']} грн"
        text  = f"*{item['name']}*\n\n{item.get('desc','')}\n\n💰 {price}"
        if item.get('photo'):
            try: await ctx.bot.send_photo(chat_id=chat, photo=item['photo'])
            except Exception as e: log.warning(f"Photo: {e}")
        await q.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_item(d[5:], back))

    # ── Variant pick ──
    elif d.startswith('vc_'):
        vc = var_ctx.get(uid)
        if not vc:
            await q.edit_message_text(greet(q.from_user.first_name), parse_mode='Markdown', reply_markup=kb_main()); return
        val_idx = int(d[3:])
        key     = vc['keys'][vc['idx']]
        vals    = vc['item'].get('variants', {}).get(key, [])
        if val_idx >= len(vals): return
        vc['selected'][key] = vals[val_idx]
        vc['idx'] += 1
        if vc['idx'] < len(vc['keys']):
            nk = vc['keys'][vc['idx']]
            selected_txt = '\n'.join(f"✅ {k}: {v}" for k, v in vc['selected'].items())
            await q.edit_message_text(
                f"*{vc['item']['name']}*\n{selected_txt}\n\nОберіть {nk}:",
                parse_mode='Markdown', reply_markup=kb_variants(vc)
            )
        else:
            item  = vc['item']
            price = "Безкоштовно" if item['price'] == 0 else f"{item['price']} грн"
            vstr  = ', '.join(f"{k}: {v}" for k, v in vc['selected'].items())
            if item.get('photo'):
                try: await ctx.bot.send_photo(chat_id=chat, photo=item['photo'])
                except Exception as e: log.warning(f"Photo: {e}")
            await q.edit_message_text(
                f"*{item['name']}*\n_{vstr}_\n\n{item.get('desc','')}\n\n💰 {price}",
                parse_mode='Markdown', reply_markup=kb_item(vc['item_id'], vc['back'])
            )

    # ── Add to cart ──
    elif d.startswith('addcart_'):
        item, _ = find_item(d[8:])
        if item:
            variants = var_ctx.pop(uid, {}).get('selected', {})
            cart_add(uid, item, variants)
            vstr = f" ({', '.join(variants.values())})" if variants else ''
            await q.answer(f"✅ {item['name']}{vstr} — в кошику!", show_alert=False)

    # ── Cart ──
    elif d.startswith('inc_'):
        cart_bump(uid, int(d[4:]), +1)
        await q.edit_message_text(f"🛒 *Ваш кошик:*\n\n{cart_text(uid)}", parse_mode='Markdown', reply_markup=kb_cart(uid))
    elif d.startswith('dec_'):
        cart_bump(uid, int(d[4:]), -1)
        await q.edit_message_text(f"🛒 *Ваш кошик:*\n\n{cart_text(uid)}", parse_mode='Markdown', reply_markup=kb_cart(uid))

    elif d == 'cart':
        await q.edit_message_text(f"🛒 *Ваш кошик:*\n\n{cart_text(uid)}", parse_mode='Markdown', reply_markup=kb_cart(uid))

    elif d == 'clearcart':
        cart_clear(uid); states.pop(uid, None)
        await q.edit_message_text('🗑 Кошик очищено.', reply_markup=kb_main())

    elif d == 'repeat':
        last = load_f('last_orders.json', {}).get(str(uid))
        if not last:
            await q.answer('Немає попереднього замовлення', show_alert=True); return
        cart_clear(uid)
        for e in last:
            item, _ = find_item(e['item_id'])
            if item:
                for _ in range(e['qty']): cart_add(uid, item, e.get('variants', {}))
        await q.edit_message_text(f"🔄 *Повторне замовлення:*\n\n{cart_text(uid)}", parse_mode='Markdown', reply_markup=kb_cart(uid))

    # ── Checkout flow ──
    elif d == 'checkout':
        if not get_cart(uid):
            await q.answer('Кошик порожній', show_alert=True); return
        pending[uid] = {'ld': 0, 'pd_pct': 0, 'note': '', '_pts': 0}
        pts = pts_get(uid) if LYL_ON else 0
        if LYL_ON and pts >= LYL_MIN:
            uah = pts_to_uah(pts)
            await q.edit_message_text(
                f"🎁 *У вас {pts} балів ≈ {uah} грн*\n\nВикористати для знижки?",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"✅ Так, −{uah} грн", callback_data='use_pts')],
                    [InlineKeyboardButton('⏭ Ні', callback_data='skip_pts')],
                ])
            )
        elif PROMOS: await _ask_promo(q, uid)
        else:        await _ask_note(q, uid)

    elif d == 'use_pts':
        pts = pts_get(uid)
        pending.setdefault(uid, {}).update({'ld': pts_to_uah(pts), '_pts': pts})
        if PROMOS: await _ask_promo(q, uid)
        else:      await _ask_note(q, uid)

    elif d == 'skip_pts':
        if PROMOS: await _ask_promo(q, uid)
        else:      await _ask_note(q, uid)

    elif d == 'skip_promo':
        states.pop(uid, None)
        await _ask_note(q, uid)

    elif d == 'skip_note':
        states.pop(uid, None)
        await _finish(q, ctx, uid)

    # ── Address ──
    elif d == 'address':
        addr = CFG.get('address', {})
        await q.edit_message_text(
            f"📍 *{CFG.get('name','')}*\n\n🗺 {addr.get('street','')}\n⏰ {addr.get('hours','')}\n📞 {addr.get('phone','')}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Назад', callback_data='back_main')]])
        )

    # ── FAQ ──
    elif d == 'faq':
        faq  = CFG.get('faq', [])
        rows = [[InlineKeyboardButton(f['q'], callback_data=f"faqitem_{i}")] for i, f in enumerate(faq)]
        rows.append([InlineKeyboardButton('⬅️ Назад', callback_data='back_main')])
        await q.edit_message_text('❓ *Часті питання:*', parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(rows))

    elif d.startswith('faqitem_'):
        idx = int(d[8:])
        faq = CFG.get('faq', [])
        if idx < len(faq):
            f = faq[idx]
            await q.edit_message_text(f"❓ *{f['q']}*\n\n{f['a']}", parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('⬅️ До питань', callback_data='faq')],
                    [InlineKeyboardButton('🏠 Головна',   callback_data='back_main')],
                ]))

# ── Checkout helpers ──────────────────────────────────────────────
async def _ask_promo(q, uid):
    states[uid] = 'promo'
    await q.edit_message_text(
        "🏷 *Є промокод?*\n\nВведіть або натисніть «Пропустити»:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⏭ Пропустити', callback_data='skip_promo')]])
    )

async def _ask_note(q, uid):
    states[uid] = 'note'
    await q.edit_message_text(
        "📝 *Коментар до замовлення:*\n\nАдреса доставки, побажання або «без коментаря»:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⏭ Без коментаря', callback_data='skip_note')]])
    )

async def _finish(q_or_msg, ctx, uid, is_msg=False):
    cart = get_cart(uid)
    user = q_or_msg.from_user
    p    = pending.get(uid, {})
    ld   = p.get('ld', 0)
    pd   = p.get('pd_pct', 0)
    note = p.get('note', '')
    pts  = p.get('_pts', 0)
    sub  = cart_sub(uid)
    after_ld  = max(0, sub - ld)
    promo_uah = round(after_ld * pd / 100) if pd else 0
    total     = max(0, after_ld - promo_uah)
    username  = f"@{user.username}" if user.username else f"ID:{user.id}"

    items_txt = '\n'.join(
        f"  • {e['item']['name']}"
        + (f" ({', '.join(e['variants'].values())})" if e.get('variants') else '')
        + f" ×{e['qty']} — {e['item']['price']*e['qty']} грн"
        for e in cart
    )
    order_msg = (
        f"🛒 *Нове замовлення — {CFG.get('name','')}*\n\n"
        f"👤 {user.full_name} ({username})\n\n"
        f"{items_txt}\n\n💰 Підсумок: {sub} грн"
    )
    if ld:        order_msg += f"\n🎁 Бали: −{ld} грн"
    if promo_uah: order_msg += f"\n🏷 Промокод −{pd}%: −{promo_uah} грн"
    order_msg += f"\n✅ *До сплати: {total} грн*"
    if note:      order_msg += f"\n\n📝 {note}"

    for admin in ALL_ADMINS:
        try: await ctx.bot.send_message(chat_id=admin, text=order_msg, parse_mode='Markdown')
        except Exception as e: log.warning(f"Admin {admin}: {e}")

    save_order(user, cart, total, ld, pd, promo_uah, note)

    last_db = load_f('last_orders.json', {})
    last_db[str(uid)] = [{'item_id': e['item']['id'], 'qty': e['qty'], 'variants': e.get('variants', {})} for e in cart]
    save_f('last_orders.json', last_db)

    if LYL_ON:
        if pts: pts_use(uid, pts)
        pts_add(uid, total)
        earned = int(total / 100 * LYL_RATE)
    else:
        earned = 0

    cart_clear(uid)
    pending.pop(uid, None)
    states.pop(uid, None)

    pay_btn = []
    if PAYMENT.get('enabled') and PAYMENT.get('monobank_jar'):
        pay_btn = [InlineKeyboardButton(f"💳 Оплатити {total} грн (Monobank)", url=PAYMENT['monobank_jar'])]

    kb = InlineKeyboardMarkup(
        ([pay_btn] if pay_btn else []) +
        [[InlineKeyboardButton('🏠 На головну', callback_data='back_main')]]
    )
    confirm = (
        f"✅ *Замовлення прийнято!*\n\n"
        f"До сплати: *{total} грн*"
        + (f"\n🎁 Нараховано {earned} балів" if earned else "")
        + "\n\nЗ вами зв'яжуться найближчим часом. Дякуємо! 🙏"
    )
    if is_msg:
        await q_or_msg.reply_text(confirm, parse_mode='Markdown', reply_markup=kb)
    else:
        await q_or_msg.edit_message_text(confirm, parse_mode='Markdown', reply_markup=kb)

# ── Text handler ──────────────────────────────────────────────────
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = states.get(uid)

    if state == 'broadcast':
        if str(uid) not in ALL_ADMINS:
            states.pop(uid, None); return
        txt   = update.message.text
        states.pop(uid, None)
        users = load_f('users.json', {'users': {}}).get('users', {})
        ok = err = 0
        prog = await update.message.reply_text(f"📢 Надсилаю {len(users)} клієнтам...")
        for user_id in users:
            if user_id == str(uid): continue
            try:
                await ctx.bot.send_message(chat_id=int(user_id), text=txt)
                ok += 1
            except: err += 1
            await asyncio.sleep(0.05)
        await prog.edit_text(f"✅ Надіслано: {ok} | Помилок: {err}")
        return

    if state == 'promo':
        code = update.message.text.strip().upper()
        if code in PROMOS:
            disc = PROMOS[code]
            pending.setdefault(uid, {})['pd_pct'] = disc
            states[uid] = 'note'
            ld       = pending[uid].get('ld', 0)
            sub_ld   = max(0, cart_sub(uid) - ld)
            promo_uah = round(sub_ld * disc / 100)
            await update.message.reply_text(
                f"✅ Промокод *{code}* — −{disc}% (−{promo_uah} грн)\n\n"
                f"📝 Додайте коментар або натисніть «Без коментаря»:",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⏭ Без коментаря', callback_data='skip_note')]])
            )
        else:
            await update.message.reply_text(
                "❌ Промокод не знайдено. Спробуйте ще або пропустіть.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⏭ Пропустити', callback_data='skip_promo')]])
            )
        return

    if state == 'note':
        pending.setdefault(uid, {})['note'] = update.message.text.strip()
        states.pop(uid, None)
        await _finish(update.message, ctx, uid, is_msg=True)
        return

    await update.message.reply_text('Скористайтесь кнопками меню 👇', reply_markup=kb_main())

# ── Mini App order handler ────────────────────────────────────────
async def on_webapp_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw  = update.message.web_app_data.data
    user = update.effective_user
    reg_user(user)
    try:
        data = json.loads(raw)
    except Exception:
        await update.message.reply_text('❌ Помилка даних із міні-апс.'); return

    items = data.get('items', [])
    note  = data.get('note', '')
    total = data.get('total', 0)
    if not items:
        await update.message.reply_text('Кошик порожній.'); return

    username  = f"@{user.username}" if user.username else f"ID:{user.id}"
    items_txt = '\n'.join(
        f"  • {it['name']} (EU {it.get('size','?')}) ×{it['qty']} — {it['price']*it['qty']} грн"
        for it in items
    )
    order_msg = (
        f"🛒 *Нове замовлення (Mini App) — {CFG.get('name','')}*\n\n"
        f"👤 {user.full_name} ({username})\n\n"
        f"{items_txt}\n\n"
        f"✅ *До сплати: {total} грн*"
    )
    if note:
        order_msg += f"\n\n📝 {note}"

    for admin in ALL_ADMINS:
        try: await ctx.bot.send_message(chat_id=admin, text=order_msg, parse_mode='Markdown')
        except Exception as e: log.warning(f"Admin notify {admin}: {e}")

    db = load_f('orders.json', {'orders': []})
    db['orders'].append({
        'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'source': 'mini_app',
        'user': {'name': user.full_name, 'id': user.id, 'username': username},
        'items': items, 'total': total, 'note': note
    })
    save_f('orders.json', db)

    earned = 0
    if LYL_ON:
        pts_add(user.id, total)
        earned = int(total / 100 * LYL_RATE)

    pay_btn = []
    if PAYMENT.get('enabled') and PAYMENT.get('monobank_jar'):
        pay_btn = [[InlineKeyboardButton(f"💳 Оплатити {total} грн (Monobank)", url=PAYMENT['monobank_jar'])]]

    await update.message.reply_text(
        f"✅ *Замовлення прийнято!*\n\n"
        f"До сплати: *{total} грн* (накладений платіж НП)\n"
        + (f"\n🎁 Нараховано {earned} балів лояльності" if earned else "")
        + "\n\nЗ вами зв'яжуться найближчим часом. Дякуємо! 🙏",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(pay_btn + [[InlineKeyboardButton('🏠 На головну', callback_data='back_main')]])
    )

# ── Run ───────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise ValueError('BOT_TOKEN не встановлено! Запуск: BOT_TOKEN=xxx python bot.py')
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start',     cmd_start))
    app.add_handler(CommandHandler('stats',     cmd_stats))
    app.add_handler(CommandHandler('points',    cmd_points))
    app.add_handler(CommandHandler('broadcast', cmd_broadcast))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_webapp_order))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info(f"WOW Bots v4.0 MAX: {CFG.get('name','—')}")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
