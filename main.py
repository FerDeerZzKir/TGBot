import asyncio
import logging
import os
import httpx
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('BOT_TOKEN')
BASE_URL = "http://apilearning.railway.internal:8080/api"

bot = Bot(token=TOKEN)
dp = Dispatcher()

class RegistrationStates(StatesGroup):
    waiting_for_role = State()
    waiting_for_group_search = State()
    waiting_for_lecturer_search = State()

# кеш
time_to_pair = {}
user_week_view = {}
system_time_cache = {}

WEEKDAYS_INDEX = {1: "Пн", 2: "Вв", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Нд"}

# функції

def parse_full_name(full_name: str):
    parts = full_name.split()
    ln = parts[0] if len(parts) > 0 else ""     
    fn = parts[1] if len(parts) > 1 else ""     
    sn = parts[2] if len(parts) > 2 else ""   
    return fn, ln, sn

def get_role_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Студент 🎓", callback_data="set_role:student")
    builder.button(text="Викладач 👨‍🏫", callback_data="set_role:lecturer")
    return builder.as_markup()

def get_days_keyboard(user_id: int, view_week: int, system_time: dict):
    builder = InlineKeyboardBuilder()
    real_current_week_abs = system_time.get('currentWeek', 1)
    real_parity = 1 if real_current_week_abs % 2 != 0 else 2
    real_current_day = system_time.get('currentDay', 0) 

    
    days = [("Пн", "day|Пн", 1), ("Вв", "day|Вв", 2), ("Ср", "day|Ср", 3),
            ("Чт", "day|Чт", 4), ("Пт", "day|Пт", 5), ("Сб", "day|Сб", 6)]

    for text, callback, idx in days:
        display_text = f"🔵 {text}" if (idx == real_current_day and view_week == real_parity) else text
        builder.button(text=display_text, callback_data=callback)

    next_w = 2 if view_week == 1 else 1
    builder.button(text=f"🔄 На {next_w}-й тиждень", callback_data=f"switch_week|{next_w}")
    builder.button(text="👤 Профіль", callback_data="show_profile")
    builder.adjust(3, 3, 1, 1)
    return builder.as_markup()

def get_profile_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Змінити дані", callback_data="change_role")
    builder.button(text="📅 Назад до розкладу", callback_data="back_to_schedule")
    builder.adjust(1)
    return builder.as_markup()

async def get_system_time():
    now = datetime.now()
    if "data" not in system_time_cache or (now - system_time_cache.get("last_updated", now)) > timedelta(minutes=5):
        async with httpx.AsyncClient(verify=False) as client:
            try:
                resp = await client.get(f"{BASE_URL}/schedule/time/current", timeout=5.0)
                if resp.status_code == 200:
                    system_time_cache["data"] = resp.json()
                    system_time_cache["last_updated"] = now
            except Exception: pass
    return system_time_cache.get("data", {"currentWeek": 1, "currentDay": 0, "currentLesson": 0})

async def perform_registration(u: types.User, role: str, g_id=0, g_name=None, g_faculty="KPI", l_id=None, fn=None, ln=None, sn=""):
    async with httpx.AsyncClient(verify=False) as client:
        try:
            if role == "student" and g_id:
                group_payload = {
                    "id": int(g_id), 
                    "groupName": str(g_name or "Unknown"), 
                    "faculty": str(g_faculty)
                }
                logger.info(f"Adding group to DB: {group_payload}")
                
                g_resp = await client.post(f"{BASE_URL}/groups/add", json=group_payload)
                if g_resp.status_code not in [200, 201]:
                    logger.warning(f"Group add info: {g_resp.status_code} - {g_resp.text}")
            
            user_data = {
                "userId": int(u.id),
                "username": str(u.username or ""),
                "firstName": str(fn) if fn else str(u.first_name or "Unknown"),
                "lastName": str(ln) if ln else str(u.last_name or ""),
                "surName": str(sn) if sn else "",
                "role": str(role),
                "groupId": int(g_id) if role == "student" else None,
                "lecturerId": str(l_id) if (role == "lecturer" and l_id) else None,
                "lastMessageId": int(u.id)
            }

            logger.info(f"Syncing user {u.id}: {user_data}")

            check = await client.get(f"{BASE_URL}/users/{u.id}")
            
            if check.status_code == 404:
                resp = await client.post(f"{BASE_URL}/users/create", json=user_data)
            else:
                resp = await client.patch(f"{BASE_URL}/users/patch/{u.id}", json=user_data)
            
            if resp.status_code not in [200, 201]:
                logger.error(f"API Error {resp.status_code}: {resp.text}")
                return False
                
            return True
            
        except Exception as e: 
            logger.error(f"Critical error in registration: {e}")
            return False

async def get_schedule_text(user_id: int, selected_day: str, view_week: int):
    s_time = await get_system_time()
    real_week_abs = s_time.get('currentWeek', 1)
    current_parity = 1 if real_week_abs % 2 != 0 else 2
    
    today_dt = datetime.now().date()
    type_map = {"Лек": "Лекція", "Прак": "Практика", "Лаб": "Лабораторна"}
    
    async with httpx.AsyncClient(verify=False) as client:  

        u_resp = await client.get(f"{BASE_URL}/users/{user_id}")  
        if u_resp.status_code != 200: return None, real_week_abs  
        
        user = u_resp.json()  
        role = user.get('role', 'student')  
        target_id = user.get('lecturerId' if role == 'lecturer' else 'groupId', 0)
        
        url = f"{BASE_URL}/schedule/{'lecturer' if role == 'lecturer' else 'lessons'}/{target_id}"  
        resp = await client.get(url)  
        if resp.status_code != 200: return "❌ Помилка завантаження", real_week_abs  
        
        full_schedule = resp.json()  
        week_key = "scheduleFirstWeek" if view_week == 1 else "scheduleSecondWeek"  
        day_data = next((d for d in full_schedule.get(week_key, []) if d['day'] == selected_day), None)  
    
        days_map = {"Пн": 0, "Вв": 1, "Ср": 2, "Чт": 3, "Пт": 4, "Сб": 5, "Нд": 6}  
        start_of_this_week = today_dt - timedelta(days=today_dt.weekday())
        weeks_diff = view_week - current_parity
        if weeks_diff < 0: weeks_diff += 2 
        
        view_date = start_of_this_week + timedelta(days=days_map.get(selected_day, 0) + (weeks_diff * 7))
        
        msg = f"📅 *{selected_day} • {view_date.strftime('%d.%m')}*\n🗓 Тиждень: *{view_week}*\n\n"  
        
        if not day_data or not day_data.get('pairs'): 
            return msg + "🎉 _Пар немає_", real_week_abs  

        merged_pairs = {} 
        
        for p in day_data['pairs']:
            dates = p.get('dates', [])
            dates_info = ""
            if dates:
                valid_dates = [datetime.strptime(d, "%Y-%m-%d").date() for d in dates]
                if len(valid_dates) > 0 and max(valid_dates) < today_dt:
                    continue 
                left = len([d for d in valid_dates if d >= today_dt])
                dates_info = f" (Зал: {left})"

            p_time = p['time']
            p_num = time_to_pair.get(p_time, "?")
            
            raw_groups = p.get('groups', [])
            p_groups = []
            if isinstance(raw_groups, list):
                for g in raw_groups:
                    name = g.get('name') if isinstance(g, dict) else str(g)
                    if name: p_groups.append(name)
            elif raw_groups:
                p_groups.append(str(raw_groups))

            if p_time not in merged_pairs:
                merged_pairs[p_time] = {
                    "num": p_num, 
                    "time": p_time[:5], 
                    "names": [p['name']],
                    "types": [type_map.get(p.get('type'), p.get('type'))],
                    "lecturers": [p.get('lecturer', {}).get('name', 'Не вказано')],
                    "locations": [p.get('location', {}).get('title') if p.get('location') else "Дистанційно"],
                    "groups": p_groups,
                    "dates_info": dates_info
                }
            else:
                m = merged_pairs[p_time]
                
                if p['name'] not in m["names"]: m["names"].append(p['name'])
                
                l_name = p.get('lecturer', {}).get('name', 'Не вказано')
                if l_name not in m["lecturers"]: m["lecturers"].append(l_name)
                
                p_type = type_map.get(p.get('type'), p.get('type'))
                if p_type not in m["types"]: m["types"].append(p_type)
                
                loc = p.get('location', {}).get('title') if p.get('location') else "Дистанційно"
                if loc not in m["locations"]: m["locations"].append(loc)
                
                for g in p_groups:
                    if g not in m["groups"]: m["groups"].append(g)

     
        sorted_times = sorted(merged_pairs.keys())
        pairs_found = 0
        
        for t_key in sorted_times:
            p = merged_pairs[t_key]
            pairs_found += 1
            
            is_now = ""
            if (str(p['num']) == str(s_time.get('currentLesson')) and 
                view_week == current_parity and 
                selected_day == WEEKDAYS_INDEX.get(s_time.get('currentDay'))):
                is_now = " 🟢"

            msg += f"⏰ {p['time']} — {p['num']} пара{is_now}\n📘 *{' / '.join(p['names'])}*{p['dates_info']}\n"  
            msg += f"🎭 {' / '.join(set(p['types']))}\n"  
            
         
            if role == 'lecturer': 
                msg += f"👥 Гр: {', '.join(p['groups']) if p['groups'] else 'Не вказано'}\n"
            else: 
                msg += f"👨‍🏫 {' / '.join(p['lecturers'])}\n"  
              
            loc_icons = []
            for l in p['locations']:
                icon = '💻' if any(w in l.lower() for w in ['дистанц', 'zoom', 'google', 'meet', 'online']) else '🏫'
                loc_icons.append(f"{icon} {l}")
            
            msg += f"{' / '.join(loc_icons)}\n──────────────\n"  
              
        if pairs_found == 0:
            return msg + "🎉 _Пар немає (закінчилися)_", real_week_abs
            
        return msg, real_week_abs

async def get_profile_data(user_id: int):
    async with httpx.AsyncClient(verify=False) as client:
        u_resp = await client.get(f"{BASE_URL}/users/{user_id}")
        if u_resp.status_code != 200: return None
        u = u_resp.json()
        role = u.get('role')
        role_label = "Викладач 👨‍🏫" if role == 'lecturer' else "Студент 🎓"
        full_name = f"{u.get('lastName', '')} {u.get('firstName', '')} {u.get('surName', '')}".strip()
        text = f"👤 *Мій профіль*\n━━━━━━━━━━━━━━\n📝 *ПІБ:* {full_name}\n🎭 *Роль:* {role_label}\n"
        if role == 'student':
            g_id = u.get('groupId')
            if g_id:
                g_resp = await client.get(f"{BASE_URL}/groups/{g_id}")
                if g_resp.status_code == 200:
                    g_data = g_resp.json()
                    res = g_data[0] if isinstance(g_data, list) else g_data
                    text += f"👥 *Група:* {res.get('group_name') or res.get('name')}\n🏢 *Факультет:* {res.get('faculty', 'KPI')}\n"
        else:
            text += f"🆔 *ID:* `{u.get('lecturerId', '?')[:15]}...`\n"
        return text + "━━━━━━━━━━━━━━"

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.get(f"{BASE_URL}/users/{message.from_user.id}")
        if resp.status_code == 404:
            await message.answer(f"Привіт! Оберіть свою роль:", reply_markup=get_role_keyboard())
            await state.set_state(RegistrationStates.waiting_for_role)
        else:
            s_time = await get_system_time()
            uid = message.from_user.id
            
            cur_day_idx = s_time.get('currentDay', 1)
            cur_week_abs = s_time.get('currentWeek', 1)
            
            view_week = 1 if cur_week_abs % 2 != 0 else 2
            today_name = WEEKDAYS_INDEX.get(cur_day_idx, "Пн")
            
            if cur_day_idx == 7: 
                today_name = "Пн"
                view_week = 2 if view_week == 1 else 1 
            
            user_week_view[uid] = view_week
            text, _ = await get_schedule_text(uid, today_name, view_week)
            await message.answer(text, reply_markup=get_days_keyboard(uid, view_week, s_time), parse_mode="Markdown")

@dp.callback_query(F.data == "show_profile")
async def show_profile_cb(callback: types.CallbackQuery):
    text = await get_profile_data(callback.from_user.id)
    if text: await callback.message.edit_text(text, reply_markup=get_profile_keyboard(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "back_to_schedule")
async def back_to_schedule_cb(callback: types.CallbackQuery):
    uid, s_time = callback.from_user.id, await get_system_time()
    cur_week_abs = s_time.get('currentWeek', 1)
    if uid not in user_week_view: user_week_view[uid] = 1 if cur_week_abs % 2 != 0 else 2
    today_name = WEEKDAYS_INDEX.get(s_time.get('currentDay'), "Пн")
    if today_name == "Нд": today_name = "Пн"
    text, _ = await get_schedule_text(uid, today_name, user_week_view[uid])
    await callback.message.edit_text(text, reply_markup=get_days_keyboard(uid, user_week_view[uid], s_time), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "change_role")
async def change_role_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Оберіть роль:", reply_markup=get_role_keyboard())
    await state.set_state(RegistrationStates.waiting_for_role)
    await callback.answer()

@dp.callback_query(RegistrationStates.waiting_for_role, F.data.startswith("set_role:"))
async def set_role_cb(callback: types.CallbackQuery, state: FSMContext):
    role = callback.data.split(":")[1]
    await state.update_data(role=role)
    if role == "student":
        await callback.message.edit_text("🎓 Введіть назву групи (напр. `ІК-11`):", parse_mode="Markdown")
        await state.set_state(RegistrationStates.waiting_for_group_search)
    else:
        await callback.message.edit_text("👨‍🏫 Введіть ваше Прізвище:")
        await state.set_state(RegistrationStates.waiting_for_lecturer_search)
    await callback.answer()

@dp.message(RegistrationStates.waiting_for_group_search)
async def search_group(message: types.Message, state: FSMContext):
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.get(f"{BASE_URL}/custom/group/search", params={"name": message.text.strip()})
        if resp.status_code != 200: return await message.answer("Помилка API")
        data = resp.json()
        if data.get("type") == "exact":
            g = data.get("result")
            await perform_registration(message.from_user, "student", g_id=g["id"], g_name=g["name"], g_faculty=g.get("faculty", "KPI"))
            await state.clear()
            return await message.answer(f"✅ Група {g['name']} обрана. /start")
        results = data.get("results", [])
        if results:
            kb = InlineKeyboardBuilder()
            for g in results[:6]:
                kb.button(text=f"{g['name']} ({g.get('faculty', 'KPI')})", callback_data=f"reg_g:{g['id']}:{g.get('faculty','KPI')[:15]}:{g['name'][:15]}")
            kb.adjust(1)
            await message.answer("🔍 Оберіть групу:", reply_markup=kb.as_markup())
        else: await message.answer("Групу не знайдено.")

@dp.callback_query(F.data.startswith("reg_g:"))
async def reg_group_cb(callback: types.CallbackQuery, state: FSMContext):
    p = callback.data.split(":")
    await perform_registration(callback.from_user, "student", g_id=int(p[1]), g_faculty=p[2], g_name=p[3])
    await state.clear()
    await callback.message.edit_text(f"✅ Успішно! /start")

@dp.message(RegistrationStates.waiting_for_lecturer_search)
async def search_lecturer(message: types.Message, state: FSMContext):
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.get(f"{BASE_URL}/custom/lecturer/search", params={"fullName": message.text.strip()})
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", []) if data.get("type") != "exact" else [data.get("result")]
            if results:
                kb = InlineKeyboardBuilder()
                for l in results[:5]: kb.button(text=l["name"], callback_data=f"reg_l:{l['id'][:25]}")
                kb.adjust(1)
                await state.update_data(lecturers=results)
                await message.answer("Оберіть себе:", reply_markup=kb.as_markup())
            else: await message.answer("Не знайдено.")

@dp.callback_query(F.data.startswith("reg_l:"))
async def reg_lecturer_cb(callback: types.CallbackQuery, state: FSMContext):
    l_short_id = callback.data.split(":")[1]
    s_data = await state.get_data()
    lecturers = s_data.get("lecturers", [])
    l_obj = next((l for l in lecturers if l["id"].startswith(l_short_id)), None)
    if l_obj:
        fn, ln, sn = parse_full_name(l_obj['name'])
        if await perform_registration(callback.from_user, "lecturer", l_id=l_obj["id"], fn=fn, ln=ln, sn=sn):
            await state.clear()
            await callback.message.edit_text(f"✅ Вітаю, {l_obj['name']}! /start")
    await callback.answer()

@dp.callback_query(F.data.startswith("day|"))
async def show_day_cb(callback: types.CallbackQuery):
    day, uid = callback.data.split("|")[1], callback.from_user.id
    s_time = await get_system_time()
    view_w = user_week_view.get(uid, 1)
    text, _ = await get_schedule_text(uid, day, view_w)
    if text: await callback.message.edit_text(text, reply_markup=get_days_keyboard(uid, view_w, s_time), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("switch_week|"))
async def switch_week_cb(callback: types.CallbackQuery):
    new_w, uid = int(callback.data.split("|")[1]), callback.from_user.id
    s_time = await get_system_time()
    user_week_view[uid] = new_w
    text, _ = await get_schedule_text(uid, "Пн", new_w)
    if text: await callback.message.edit_text(text, reply_markup=get_days_keyboard(uid, new_w, s_time), parse_mode="Markdown")
    await callback.answer()

async def main():
    async with httpx.AsyncClient(verify=False) as client:
        try:
            r = await client.get(f"{BASE_URL}/schedule/slots")
            if r.status_code == 200:
                global time_to_pair
                time_to_pair = {v: k for k, v in r.json().items()}
        except: pass
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
