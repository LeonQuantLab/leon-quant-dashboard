import streamlit as st
from streamlit_option_menu import option_menu
import streamlit.components.v1 as components
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os
import time
import pymssql
import hashlib
import warnings
import akshare as ak
import json
from dotenv import load_dotenv
from datetime import datetime
from streamlit_autorefresh import st_autorefresh

# 屏蔽烦人的 pandas 警告
warnings.filterwarnings("ignore", category=UserWarning, module="pandas")

# ==========================================
# 1. 页面基础配置与安全防护
# ==========================================
st.set_page_config(page_title="Leon量化实验室", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")

if os.path.exists(".env"):
    load_dotenv()

def get_db_config():
    try:
        if os.getenv("DB_SERVER"):
            return os.getenv("DB_SERVER"), os.getenv("DB_PORT"), os.getenv("DB_USER"), os.getenv("DB_PASSWORD"), os.getenv("DB_NAME")
        elif "DB_SERVER" in st.secrets:
            return st.secrets["DB_SERVER"], st.secrets["DB_PORT"], st.secrets["DB_USER"], st.secrets["DB_PASSWORD"], st.secrets["DB_NAME"]
        return None, None, None, None, None
    except:
        return None, None, None, None, None

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

BENCHMARKS = {
    "沪深300": "SHSE.000300",
    "中证500": "SHSE.000905",
    "中证1000": "SHSE.000852",
    "上证50": "SHSE.000016",
    "创业板指": "SZSE.399006",
    "科创50": "SHSE.000688",
    "中证红利": "SHSE.000922"
}

def get_akshare_symbol(code):
    if code.startswith("SHSE"):
        return "sh" + code.split(".")[1]
    elif code.startswith("SZSE"):
        return "sz" + code.split(".")[1]
    return code

# ==========================================
# 2. 数据库交互逻辑
# ==========================================
def get_db_connection():
    s, p, u, pw, d = get_db_config()
    return pymssql.connect(server=s, port=p, user=u, password=pw, database=d, charset='utf8')

def login_user(username, password):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        pwd_hash = hash_password(password)
        cursor.execute("SELECT id FROM dbo.users WHERE username = %s AND password_hash = %s", (username, pwd_hash))
        user = cursor.fetchone()
        conn.close()
        return user[0] if user else None
    except Exception as e:
        return None

def register_user(username, password):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        pwd_hash = hash_password(password)
        
        # 先查一下有没有重名
        cursor.execute("SELECT id FROM dbo.users WHERE username = %s", (username,))
        if cursor.fetchone():
            conn.close()
            return False, "该用户名已被注册，请换一个试试！"

        cursor.execute("INSERT INTO dbo.users (username, password_hash) VALUES (%s, %s)", (username, pwd_hash))
        conn.commit()
        
        cursor.execute("SELECT id FROM dbo.users WHERE username = %s", (username,))
        user_id = cursor.fetchone()[0]
        cursor.execute("INSERT INTO dbo.strategies (user_id, name, benchmark) VALUES (%s, %s, %s)", (user_id, f"{username}的默认策略", "SHSE.000300"))
        conn.commit()
        conn.close()
        return True, "注册成功！"
    except pymssql.IntegrityError:
        return False, "该用户名已被注册，请换一个试试！"
    except Exception as e:
        return False, f"注册失败: {e}"

def get_user_strategies(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, benchmark FROM dbo.strategies WHERE user_id = %s ORDER BY id ASC", (user_id,))
        strategies = cursor.fetchall()
        conn.close()
        return strategies
    except Exception:
        return []

def create_new_strategy(user_id, strategy_name, bench_code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO dbo.strategies (user_id, name, benchmark) VALUES (%s, %s, %s)", (user_id, strategy_name, bench_code))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        st.error(f"创建策略失败: {e}")
        return False

def load_raw_nav_data(strategy_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = f"SELECT id, trade_date, strategy_nav, benchmark_nav FROM dbo.nav_history WHERE strategy_id = {strategy_id} ORDER BY trade_date"
        df = pd.read_sql(query, conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

def load_data_from_cloud(strategy_id):
    try:
        df = load_raw_nav_data(strategy_id)
        if df.empty:
            return pd.DataFrame()

        df['日期'] = pd.to_datetime(df['trade_date'], errors='coerce')
        df['策略权益'] = pd.to_numeric(df['strategy_nav'], errors='coerce')
        df['基准净值'] = pd.to_numeric(df['benchmark_nav'], errors='coerce')
        
        df = df[(df['策略权益'] > 100) & (df['基准净值'] > 100)]
        df = df.sort_values('日期')
        df['pct_change'] = df['策略权益'].pct_change()
        df = df[(df['pct_change'].fillna(0) > -0.5)]
        df = df.dropna(subset=['日期', '策略权益', '基准净值'])

        if df.empty:
            return pd.DataFrame()

        df['策略累计收益'] = (df['策略权益'] / df['策略权益'].iloc[0] - 1) * 100
        df['基准累计收益'] = (df['基准净值'] / df['基准净值'].iloc[0] - 1) * 100
        df['超额收益'] = df['策略累计收益'] - df['基准累计收益']
        return df
    except Exception:
        return pd.DataFrame()

def insert_manual_nav(strategy_id, bench_code, input_date, input_nav):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE dbo.strategies SET benchmark = %s WHERE id = %s", (bench_code, strategy_id))
        
        cursor.execute("SELECT close_price FROM dbo.benchmarks WHERE code = %s AND trade_date = %s", (bench_code, str(input_date)))
        bench_row = cursor.fetchone()
        
        if bench_row:
            bench_val = float(bench_row[0])
        else:
            try:
                ak_code = get_akshare_symbol(bench_code)
                index_df = ak.stock_zh_index_daily(symbol=ak_code)
                index_df['date'] = pd.to_datetime(index_df['date']).dt.strftime('%Y-%m-%d')
                target_row = index_df[index_df['date'] == str(input_date)]
                if target_row.empty:
                    conn.close()
                    return False, f"❌ {input_date} 无基准点位。请确认是否为周末或节假日？"
                
                bench_val = float(target_row['close'].values[0])
                cursor.execute("INSERT INTO dbo.benchmarks (code, trade_date, close_price) VALUES (%s, %s, %s)", (bench_code, str(input_date), bench_val))
                conn.commit()
            except Exception as ak_err:
                conn.close()
                return False, f"❌ 自动拉取基准失败: {ak_err}"

        sql = """
            IF NOT EXISTS (SELECT 1 FROM dbo.nav_history WHERE strategy_id = %s AND trade_date = %s)
            INSERT INTO dbo.nav_history (strategy_id, trade_date, strategy_nav, benchmark_nav)
            VALUES (%s, %s, %s, %s);
        """
        cursor.execute(sql, (strategy_id, str(input_date), strategy_id, str(input_date), float(input_nav), bench_val))
        conn.commit()
        conn.close()
        return True, "✅ 数据录入成功！"
    except Exception as e:
        return False, f"录入失败: {e}"

# ==========================================
# 3. 状态管理
# ==========================================
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.username = "Leon量化工作室"
    st.session_state.user_id = 1 
    st.session_state.current_strategy_id = 1
    st.session_state.current_strategy_name = "大A趋势跟踪"

if "auth_action" not in st.session_state:
    st.session_state.auth_action = "none"

if "current_nav" not in st.session_state:
    st.session_state.current_nav = "首页"

# ==========================================
# 4. 全局 CSS
# ==========================================
st.markdown("""
    <style>
        .stApp { background-color: #0e1117; }
        h1, h2, h3, p, div, span { color: #e0e0e0 !important; }
        div[data-testid="stMetricValue"] { color: #00ffcc; font-size: 1.4rem !important; }
        div[data-testid="stMetricLabel"] { color: #a0a0a0; font-size: 0.9rem !important; }
        
        header[data-testid="stHeader"] { display: none !important; }

        .block-container { 
            padding-top: 2.5rem !important; 
            padding-bottom: 2rem !important; 
            max-width: 95% !important; 
        }

        .top-nav-btn button {
            width: 100% !important;
            padding: 0.3rem 1rem !important;
            height: 38px !important;
            border-radius: 5px !important;
            font-size: 14px !important;
        }

        h1 a, h2 a, h3 a, h4 a, h5 a, h6 a {
            display: none !important;
        }
        [data-testid="stHeaderActionElements"] {
            display: none !important;
        }

        /* 👇 【核心修复】修复 Selectbox 下拉菜单白底灰字的问题 */
        div[data-baseweb="select"] > div {
            background-color: #1a1c24 !important;
            border-color: #333 !important;
            color: #e0e0e0 !important;
        }
        div[data-baseweb="popover"] div[data-baseweb="menu"] {
            background-color: #1a1c24 !important;
            border: 1px solid #333 !important;
        }
        div[data-baseweb="popover"] ul {
            background-color: #1a1c24 !important;
        }
        div[data-baseweb="popover"] ul li {
            color: #e0e0e0 !important;
            background-color: #1a1c24 !important;
        }
        div[data-baseweb="popover"] ul li:hover {
            background-color: #333333 !important;
            color: #00ffcc !important;
        }
        div[data-baseweb="popover"] ul li[aria-selected="true"] {
            background-color: #333333 !important;
            color: #00ffcc !important;
        }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 5. 顶部导航栏
# ==========================================
col_logo, col_nav, col_user = st.columns([2, 4.5, 3.5], vertical_alignment="center")

with col_logo:
    st.markdown("<h2 style='color: #00ffcc; margin: 0; padding: 0; font-size: 24px;'>📈 Leon量化实验室</h2>", unsafe_allow_html=True)

with col_nav:
    menu_options = ["首页", "排行榜", "加入我们", "我的"]
    nav_choice = option_menu(
        menu_title=None,
        options=menu_options,
        icons=["house", "trophy", "person-plus", "person-fill"],
        default_index=menu_options.index(st.session_state.current_nav),
        orientation="horizontal",
        styles={
            "container": {"padding": "0!important", "background-color": "transparent", "width": "100%"},
            "icon": {"color": "#a0a0a0", "font-size": "15px"},
            "nav-link": {"font-size": "15px", "text-align": "center", "margin": "0px", "--hover-color": "#1e1e24"},
            "nav-link-selected": {"background-color": "#ff4b4b", "color": "white", "font-weight": "bold"},
        }
    )
    if nav_choice != st.session_state.current_nav:
        st.session_state.current_nav = nav_choice

with col_user:
    sub_col1, sub_col2 = st.columns([2, 1.2], vertical_alignment="center")
    
    if st.session_state.logged_in:
        with sub_col1:
            st.markdown(f"<p style='text-align: right; color: #00ffcc; font-weight: bold; margin: 0;'>👤 {st.session_state.username}</p>", unsafe_allow_html=True)
        with sub_col2:
            st.markdown('<div class="top-nav-btn">', unsafe_allow_html=True)
            if st.button("退出登录", key="logout_btn", use_container_width=True):
                st.session_state.logged_in = False
                st.session_state.username = "Leon量化工作室"
                st.session_state.user_id = 1
                st.session_state.current_strategy_id = 1
                st.session_state.current_strategy_name = "大A趋势跟踪"
                st.session_state.current_nav = "首页"
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
    else:
        with sub_col1:
            st.markdown("<p style='text-align: right; color: #a0a0a0; margin: 0;'>未登录 (演示)</p>", unsafe_allow_html=True)
        with sub_col2:
            st.markdown('<div class="top-nav-btn">', unsafe_allow_html=True)
            if st.button("🔑 登录 / 注册", key="login_btn", type="primary", use_container_width=True):
                st.session_state.auth_action = "login"
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

st.divider()

# ==========================================
# 6. 看板渲染模块
# ==========================================
def render_dashboard(strategy_id, strategy_name, author_name):
    st.title(f"📈 {strategy_name}")
    st.caption(f"🆔 策略 ID: {strategy_id} | 👤 创建者: {author_name}")

    with st.spinner("正在从阿里云拉取数据..."):
        df = load_data_from_cloud(strategy_id)

    if df.empty:
        st.warning("⚠️ 当前策略暂无有效数据。")
        return

    nav_strategy = df['策略权益'] / df['策略权益'].iloc[0]
    nav_bench = df['基准净值'] / df['基准净值'].iloc[0]
    df['策略日收益'] = nav_strategy.pct_change()
    df['基准日收益'] = nav_bench.pct_change()
    returns = df.dropna(subset=['策略日收益', '基准日收益'])

    total_return = df['策略累计收益'].iloc[-1]
    bench_return = df['基准累计收益'].iloc[-1]
    excess_return = total_return - bench_return

    df['策略累计最高'] = nav_strategy.cummax()
    df['策略回撤'] = (nav_strategy - df['策略累计最高']) / df['策略累计最高'] * 100
    max_drawdown = df['策略回撤'].min()

    df['基准累计最高'] = nav_bench.cummax()
    df['基准回撤'] = (nav_bench - df['基准累计最高']) / df['基准累计最高'] * 100
    max_bench_drawdown = df['基准回撤'].min()

    days = len(df)
    if days > 1:
        strategy_annual_return = ((nav_strategy.iloc[-1]) ** (252 / days) - 1) * 100
        bench_annual_return = ((nav_bench.iloc[-1]) ** (252 / days) - 1) * 100
        annual_vol = returns['策略日收益'].std() * np.sqrt(252) * 100
        risk_free = 2.0
        cov_matrix = np.cov(returns['策略日收益'], returns['基准日收益'])
        beta = cov_matrix[0, 1] / cov_matrix[1, 1] if cov_matrix[1, 1] > 0 else 0
        alpha = strategy_annual_return - (risk_free + beta * (bench_annual_return - risk_free))
        downside_returns = returns['策略日收益'][returns['策略日收益'] < 0]
        downside_vol = downside_returns.std() * np.sqrt(252) * 100
        sortino_ratio = (strategy_annual_return - risk_free) / downside_vol if downside_vol > 0 else 0
        excess_daily = returns['策略日收益'] - returns['基准日收益']
        tracking_error = excess_daily.std() * np.sqrt(252) * 100
        info_ratio = (strategy_annual_return - bench_annual_return) / tracking_error if tracking_error > 0 else 0
        treynor_ratio = (strategy_annual_return - risk_free) / beta if beta != 0 else 0
    else:
        strategy_annual_return = bench_annual_return = annual_vol = 0
        beta = alpha = sortino_ratio = info_ratio = treynor_ratio = 0

    sharpe_ratio = (strategy_annual_return - risk_free) / annual_vol if annual_vol > 0 else 0
    calmar_ratio = strategy_annual_return / abs(max_drawdown) if max_drawdown != 0 else 0

    st.markdown("### 🏆 核心战绩总结 (相对基准)")
    s1, s2, s3, s4, s5, s6 = st.columns(6)
    s1.metric("超额收益率", f"{excess_return:.2f}%")
    s2.metric("年化 Alpha", f"{alpha/100:.2f}") 
    s3.metric("Beta 系数", f"{beta:.2f}")
    s4.metric("信息比率", f"{info_ratio:.2f}")
    s5.metric("特雷诺比率", f"{treynor_ratio/100:.2f}")
    s6.metric("历史胜率", "85.71%") 

    st.divider()
    col_left, col_right = st.columns([1, 1.8])

    with col_left:
        st.markdown("### ⚔️ 策略 vs 基准")
        c_strat, c_bench = st.columns(2)
        with c_strat:
            st.markdown("#### 📈 策略")
            st.metric("累计收益", f"{total_return:.2f}%")
            st.metric("最大回撤", f"{max_drawdown:.2f}%")
            st.metric("年化收益", f"{strategy_annual_return:.2f}%")
            st.metric("夏普比率", f"{sharpe_ratio:.2f}")
            st.metric("卡玛比率", f"{calmar_ratio:.2f}")
        with c_bench:
            st.markdown("#### 📉 基准")
            st.metric("累计收益", f"{bench_return:.2f}%")
            st.metric("最大回撤", f"{max_bench_drawdown:.2f}%")
            st.metric("年化收益", f"{bench_annual_return:.2f}%")
            st.metric("夏普比率", "0.00") 
            st.metric("卡玛比率", "0.00") 

    with col_right:
        st.markdown("### 📊 累计收益率与回撤走势")
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.05, subplot_titles=("累计收益率对比 (%)", "回撤对比 (%)"))
        fig.add_trace(go.Scatter(x=df['日期'], y=df['策略累计收益'], mode='lines+markers', name='策略', line=dict(color='#ff4b4b', width=2.5)), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['日期'], y=df['基准累计收益'], mode='lines+markers', name='基准', line=dict(color='#4b8bff', width=1.5, dash='dot')), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['日期'], y=df['超额收益'], mode='lines+markers', name='超额', line=dict(color='#ffd700', width=1.5)), row=1, col=1)
        fig.add_trace(go.Scatter(x=df['日期'], y=df['策略回撤'], mode='lines+markers', name='策略回撤', fill='tozeroy', fillcolor='rgba(255, 75, 75, 0.2)', line=dict(color='#ff4b4b', width=1.5)), row=2, col=1)
        fig.add_trace(go.Scatter(x=df['日期'], y=df['基准回撤'], mode='lines+markers', name='基准回撤', fill='tozeroy', fillcolor='rgba(75, 139, 255, 0.1)', line=dict(color='#4b8bff', width=1.0, dash='dot')), row=2, col=1)
        fig.add_hline(y=0, line_dash="dash", line_color="#555", row=1, col=1)
        fig.update_layout(plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)', font=dict(color='#a0a0a0'), hovermode='x unified', height=650, margin=dict(l=20, r=20, t=40, b=20), legend=dict(orientation="h", yanchor="bottom", y=1.05, xanchor="right", x=1))
        fig.update_yaxes(title_text="收益率 (%)", showgrid=True, gridcolor='#222', linecolor='#333', row=1, col=1)
        fig.update_yaxes(title_text="回撤 (%)", showgrid=True, gridcolor='#222', linecolor='#333', row=2, col=1)
        if len(df) > 1:
            x_range = [df['日期'].min(), df['日期'].max()]
        else:
            x_range = [df['日期'].min() - pd.Timedelta(days=1), df['日期'].max() + pd.Timedelta(days=1)]
        
        fig.update_xaxes(
            range=x_range, 
            showgrid=False, 
            linecolor='#333', 
            hoverformat='%Y-%m-%d',
            tickformat='%Y-%m',
            dtick="M1"
        )
        st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.caption("⚠️ 免责声明：历史业绩不代表未来表现。")

# ==========================================
# 7. 路由分发
# ==========================================
if st.session_state.auth_action == "login":
    st.title("🔒 身份验证")
    tab1, tab2 = st.tabs(["登录", "注册"])
    
    with tab1:
        u = st.text_input("用户名", key="login_u")
        p = st.text_input("密码", type="password", key="login_p")
        if st.button("登录", type="primary"):
            uid = login_user(u, p)
            if uid:
                st.session_state.logged_in = True
                st.session_state.username = u
                st.session_state.user_id = uid
                st.session_state.auth_action = "none"
                st.session_state.current_nav = "我的" 
                
                strats = get_user_strategies(uid)
                if strats:
                    st.session_state.current_strategy_id = strats[0][0]
                    st.session_state.current_strategy_name = strats[0][1]
                st.rerun()
            else:
                st.error("用户名或密码错误！")
                
    with tab2:
        ru = st.text_input("设置用户名", key="reg_u")
        rp = st.text_input("设置密码", type="password", key="reg_p")
        if st.button("注册并登录", type="primary"):
            if len(ru) < 3 or len(rp) < 6:
                st.warning("用户名需大于3位，密码需大于6位！")
            else:
                success, msg = register_user(ru, rp)
                if success:
                    uid = login_user(ru, rp)
                    st.session_state.logged_in = True
                    st.session_state.username = ru
                    st.session_state.user_id = uid
                    st.session_state.auth_action = "none"
                    st.session_state.current_nav = "我的" 
                    
                    st.success(msg)
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error(msg)

    if st.button("⬅️ 取消登录，返回首页"):
        st.session_state.auth_action = "none"
        st.session_state.current_nav = "首页"
        st.rerun()
    st.stop()

if nav_choice == "首页":
    if st.session_state.logged_in:
        user_strats = get_user_strategies(st.session_state.user_id)
        strat_names = [s[1] for s in user_strats]
        if strat_names:
            selected_name = st.selectbox("切换你的策略", strat_names, index=strat_names.index(st.session_state.current_strategy_name) if st.session_state.current_strategy_name in strat_names else 0)
            for s in user_strats:
                if s[1] == selected_name:
                    st.session_state.current_strategy_id = s[0]
                    st.session_state.current_strategy_name = s[1]
                    break
            render_dashboard(st.session_state.current_strategy_id, st.session_state.current_strategy_name, st.session_state.username)
    else:
        render_dashboard(1, "大A趋势跟踪", "Leon量化工作室")

# ==========================================
# 排行榜页面 (带表头点击排序、完整历史迷你图)
# ==========================================
elif nav_choice == "排行榜":
    st.title("🏆 全站策略实盘排行榜")
    st.markdown("展示所有已入库策略的历史表现。💡 *点击表头文字可进行升序/降序切换*。")
    st.divider()

    col_main = st.container()

    with col_main:
        try:
            conn = get_db_connection()
            query_strats = """
                SELECT s.id, s.name, u.username 
                FROM dbo.strategies s
                JOIN dbo.users u ON s.user_id = u.id
                WHERE EXISTS (SELECT 1 FROM dbo.nav_history WHERE strategy_id = s.id)
            """
            df_strats = pd.read_sql(query_strats, conn)
            
            if df_strats.empty:
                st.info("排行榜暂无数据，等您来霸榜！")
            else:
                rank_data = []
                for _, row in df_strats.iterrows():
                    s_id = row['id']
                    df_nav = pd.read_sql(f"SELECT trade_date, strategy_nav FROM dbo.nav_history WHERE strategy_id = {s_id} ORDER BY trade_date ASC", conn)
                    
                    if len(df_nav) > 1:
                        navs = df_nav['strategy_nav'].values
                        days = len(df_nav)
                        gain = (navs[-1] / navs[0] - 1) * 100
                        annualized = ((navs[-1] / navs[0]) ** (252 / days) - 1) * 100
                        cum_max = np.maximum.accumulate(navs)
                        drawdowns = (navs - cum_max) / cum_max
                        max_drawdown = abs(drawdowns.min()) * 100
                        
                        sparkline_data = navs.tolist()
                        
                        rank_data.append({
                            "name": row['name'],
                            "author": row['username'],
                            "gain": round(gain, 2),
                            "annualized": round(annualized, 2),
                            "drawdown": round(max_drawdown, 2),
                            "days": days,
                            "spark": sparkline_data
                        })
                conn.close()
                
                if not rank_data:
                    st.info("暂无足够的数据生成排行榜。")
                else:
                    rank_data = sorted(rank_data, key=lambda x: x['gain'], reverse=True)
                    json_data = json.dumps(rank_data)
                    
                    html_code = f"""
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <style>
                            body {{ margin: 0; padding: 0; background-color: transparent; font-family: sans-serif; }}
                            .custom-table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
                            .custom-table th {{ text-align: left; padding: 12px; color: #a0a0a0; border-bottom: 1px solid #333; font-weight: normal; user-select: none; }}
                            .custom-table th.sortable {{ cursor: pointer; }}
                            .custom-table th.sortable:hover {{ color: #fff; }}
                            .custom-table td {{ padding: 12px; border-bottom: 1px solid #222; vertical-align: middle; color: #e0e0e0; }}
                            .custom-table tr:hover {{ background-color: #1e1e24; }}
                            .positive {{ color: #00ffcc !important; font-weight: bold; }}
                            .negative {{ color: #ff4b4b !important; font-weight: bold; }}
                            .sys-name {{ color: #ffffff; font-weight: bold; font-size: 15px; margin-bottom: 4px; }}
                            .sys-author {{ color: #a0a0a0; font-size: 12px; }}
                            .arrow {{ font-size: 12px; margin-left: 5px; color: #666; }}
                            .arrow.active {{ color: #00ffcc; }}
                        </style>
                    </head>
                    <body>
                        <table class="custom-table">
                            <thead>
                                <tr>
                                    <th>策略名称</th>
                                    <th class="sortable" onclick="sortTable('gain', true)">历史收益 <span class="arrow active" id="arr-gain">▼</span></th>
                                    <th class="sortable" onclick="sortTable('annualized', true)">年化收益 <span class="arrow" id="arr-annualized">⇅</span></th>
                                    <th class="sortable" onclick="sortTable('drawdown', true)">最大回撤 <span class="arrow" id="arr-drawdown">⇅</span></th>
                                    <th class="sortable" onclick="sortTable('days', true)">运行天数 <span class="arrow" id="arr-days">⇅</span></th>
                                    <th>收益走势 (历史)</th>
                                </tr>
                            </thead>
                            <tbody id="table-body">
                            </tbody>
                        </table>

                        <script>
                            let data = {json_data};
                            let currentSortKey = 'gain';
                            let isAscending = false;

                            function generateSparkline(sparkData, width=120, height=40) {{
                                if (sparkData.length < 2) return "";
                                let min = Math.min(...sparkData);
                                let max = Math.max(...sparkData);
                                if (max === min) max += 1;
                                
                                let points = [[0, height]];
                                for (let i = 0; i < sparkData.length; i++) {{
                                    let x = (i / (sparkData.length - 1)) * width;
                                    let y = height - ((sparkData[i] - min) / (max - min)) * height;
                                    points.push([x, y]);
                                }}
                                points.push([width, height]);
                                
                                let pathStr = "M " + points.map(p => p[0] + "," + p[1]).join(" L ") + " Z";
                                let linePoints = points.slice(1, points.length - 1).map(p => p[0] + "," + p[1]).join(" L ");
                                let linePath = "M " + linePoints;
                                
                                let isUp = sparkData[sparkData.length - 1] >= sparkData[0];
                                let strokeColor = isUp ? "#00ffcc" : "#ff4b4b";
                                let fillColor = isUp ? "rgba(0, 255, 204, 0.15)" : "rgba(255, 75, 75, 0.15)";
                                
                                return `<svg width="${{width}}" height="${{height}}" viewBox="0 0 ${{width}} ${{height}}" style="overflow: visible;">
                                            <path d="${{pathStr}}" fill="${{fillColor}}" />
                                            <path d="${{linePath}}" fill="none" stroke="${{strokeColor}}" stroke-width="1.5" stroke-linejoin="round" />
                                        </svg>`;
                            }}

                            function renderTable() {{
                                let tbody = document.getElementById("table-body");
                                tbody.innerHTML = "";
                                
                                data.forEach(item => {{
                                    let gainClass = item.gain >= 0 ? "positive" : "negative";
                                    let annualClass = item.annualized >= 0 ? "positive" : "negative";
                                    
                                    let gainStr = item.gain > 0 ? "+" + item.gain.toFixed(2) + "%" : item.gain.toFixed(2) + "%";
                                    let annualStr = item.annualized > 0 ? "+" + item.annualized.toFixed(2) + "%" : item.annualized.toFixed(2) + "%";
                                    
                                    let svgContent = generateSparkline(item.spark);
                                    
                                    let tr = document.createElement("tr");
                                    tr.innerHTML = `
                                        <td>
                                            <div class="sys-name">${{item.name}}</div>
                                            <div class="sys-author">${{item.author}}</div>
                                        </td>
                                        <td class="${{gainClass}}">${{gainStr}}</td>
                                        <td class="${{annualClass}}">${{annualStr}}</td>
                                        <td class="negative">${{item.drawdown.toFixed(2)}}%</td>
                                        <td>${{item.days}} 天</td>
                                        <td>${{svgContent}}</td>
                                    `;
                                    tbody.appendChild(tr);
                                }});
                            }}

                            function sortTable(key, isNumeric) {{
                                document.querySelectorAll('.arrow').forEach(el => {{
                                    el.className = 'arrow';
                                    el.innerText = '⇅';
                                }});

                                if (currentSortKey === key) {{
                                    isAscending = !isAscending;
                                }} else {{
                                    currentSortKey = key;
                                    isAscending = false;
                                }}

                                let arrowEl = document.getElementById("arr-" + key);
                                arrowEl.className = 'arrow active';
                                arrowEl.innerText = isAscending ? '▲' : '▼';

                                data.sort((a, b) => {{
                                    let valA = a[key];
                                    let valB = b[key];
                                    if (isNumeric) {{
                                        return isAscending ? valA - valB : valB - valA;
                                    }} else {{
                                        return isAscending ? valA.localeCompare(valB) : valB.localeCompare(valA);
                                    }}
                                }});
                                
                                renderTable();
                            }}

                            renderTable();
                        </script>
                    </body>
                    </html>
                    """
                    
                    components.html(html_code, height=600, scrolling=True)

        except Exception as e:
            st.error(f"排行榜加载失败: {e}")

# ==========================================
# 其他页面
# ==========================================

elif nav_choice == "加入我们":
    st.title("🤝 加入 Leon 量化社区")
    st.divider()
    
    st.markdown("### 关于我们")
    st.markdown("""
    **Leon量化实验室** 致力于将代码与金融完美结合，打造属于个人的量化交易终端。
    
    如果你：
    * 对量化交易、ETF轮动感兴趣
    * 希望定制专属的量化模型
    * 或者只是想每天记录一下自己的实盘净值
    
    欢迎加入我们！
    """)
    
    st.info("💡 请联系作者，备注 **“量化看板”**，领取《独家白皮书》并加入实盘交流群。")
    
    st.markdown("""
    **联系方式：**
    * 微信：`s13064387800`
    * 邮箱：`suyang0903@163.com`
    """)

elif nav_choice == "我的":
    if not st.session_state.logged_in:
        st.warning("⚠️ 您尚未登录，请点击右上角「🔑 登录/注册」。")
    else:
        st.title("👤 我的量化空间")
        st.markdown(f"欢迎回来，**{st.session_state.username}**。在这里管理你的策略与数据。")
        
        user_strats = get_user_strategies(st.session_state.user_id)
        strat_names = [s[1] for s in user_strats]
        
        tab1, tab2, tab3 = st.tabs(["📊 策略管理", "📝 录入数据", "📜 历史数据编辑"])
        
        with tab1:
            st.markdown("#### 切换或新建策略")
            if strat_names:
                selected_name = st.selectbox("当前选中策略", strat_names, index=strat_names.index(st.session_state.current_strategy_name) if st.session_state.current_strategy_name in strat_names else 0)
                for s in user_strats:
                    if s[1] == selected_name:
                        st.session_state.current_strategy_id = s[0]
                        st.session_state.current_strategy_name = s[1]
                        break
            
            st.divider()
            with st.form("new_strat_form_tab"):
                new_strat_name = st.text_input("新策略名称")
                new_bench = st.selectbox("初始基准", list(BENCHMARKS.keys()))
                if st.form_submit_button("创建策略", type="primary"):
                    if new_strat_name:
                        if create_new_strategy(st.session_state.user_id, new_strat_name, BENCHMARKS[new_bench]):
                            st.session_state.current_strategy_name = new_strat_name
                            st.success("创建成功！已为您切换。")
                            time.sleep(1)
                            st.rerun()
        
        with tab2:
            st.markdown(f"#### 录入今日数据 (策略: {st.session_state.current_strategy_name})")
            with st.form("manual_nav_input_tab"):
                input_date = st.date_input("日期", datetime.now())
                input_nav = st.number_input("今日总资产", min_value=0.01, step=1000.0, value=100000.0)
                
                current_bench_code = next((s[2] for s in user_strats if s[1] == st.session_state.current_strategy_name), "SHSE.000300")
                current_bench_name = next((k for k, v in BENCHMARKS.items() if v == current_bench_code), "沪深300")
                selected_bench_name = st.selectbox("对比基准", list(BENCHMARKS.keys()), index=list(BENCHMARKS.keys()).index(current_bench_name))
                bench_code = BENCHMARKS[selected_bench_name]
                
                if st.form_submit_button("提交数据", type="primary"):
                    success, msg = insert_manual_nav(st.session_state.current_strategy_id, bench_code, input_date, input_nav)
                    if success:
                        st.success(msg)
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error(msg)
        
        with tab3:
            st.markdown(f"#### 修改或删除历史数据 (策略: {st.session_state.current_strategy_name})")
            raw_df = load_raw_nav_data(st.session_state.current_strategy_id)
            if raw_df.empty:
                st.info("暂无历史数据")
            else:
                edit_df = raw_df.copy()
                edit_df = edit_df.rename(columns={"trade_date": "日期", "strategy_nav": "总资产", "benchmark_nav": "基准点位"})
                edit_df = edit_df[["日期", "总资产", "基准点位"]]
                
                edited_result = st.data_editor(edit_df, hide_index=True, use_container_width=True, num_rows="dynamic")
                
                if st.button("💾 保存修改并刷新", use_container_width=True, type="primary"):
                    try:
                        conn = get_db_connection()
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM dbo.nav_history WHERE strategy_id = %s", (st.session_state.current_strategy_id,))
                        for _, row in edited_result.iterrows():
                            if pd.notna(row['日期']) and pd.notna(row['总资产']) and pd.notna(row['基准点位']):
                                cursor.execute(
                                    "INSERT INTO dbo.nav_history (strategy_id, trade_date, strategy_nav, benchmark_nav) VALUES (%s, %s, %s, %s)",
                                    (st.session_state.current_strategy_id, str(row['日期']), float(row['总资产']), float(row['基准点位']))
                                )
                        conn.commit()
                        conn.close()
                        st.success("✅ 保存成功！")
                        time.sleep(1)
                        st.rerun()
                    except Exception as e:
                        st.error(f"保存失败: {e}")