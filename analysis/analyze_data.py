import analysis_main as A
import numpy as np
import pandas as pd
import pyecharts


# 函数注册器
def ways(func):
    A.Analyze.analyze_fn_list.append(func)

    def wrapper(*args, **kw):
        return func(*args, **kw)

    return wrapper


def main():
    global cursor, db, conf
    cursor = A.Analyze.cursor
    db = A.Analyze.db

    print(A.Analyze.analyze_fn_list)
    conf = A.Analyze.conf
    conf.add_section('chart')

    for fn in A.Analyze.analyze_fn_list:
        fn()
        print(fn.__name__ + '  ok')

    with open('conf.ini', 'a') as configfile:
        conf.write(configfile)


@ways
def f1():
    cursor.execute("select ave_pay,number from 传统职业 where ID <'10000'")
    re1 = cursor.fetchall()
    m = [x[0] for x in re1 if x[0] is not None and x[1] is not None]
    n = [int(x[1]) for x in re1 if x[0] is not None and x[1] is not None]
    a = []
    for i in range(0, len(m)):
        j = 0
        while j < n[i]:
            a.append(m[i])
            j += 1
    a = np.array(a)
    a1 = [a[i] for i in range(0, len(a)) if a[i] != a.max() and a[i] != a.min()]

    cursor.execute("select ave_pay,number from 新兴职业 where id <'10000'")
    re2 = cursor.fetchall()
    # # #
    m = [x[0] for x in re2 if x[0] is not None and x[1] is not None]
    n = [int(x[1]) for x in re2 if x[0] is not None and x[1] is not None]
    a = []
    for i in range(0, len(m)):
        j = 0
        while j < n[i]:
            a.append(m[i])
            j += 1
    a = np.array(a)
    b1 = [a[i] for i in range(0, len(a)) if a[i] != a.max() and a[i] != a.min()]
    q = [a1, b1]
    re1 = pyecharts.Boxplot.prepare_data(q)
    conf.set('chart', 'chart.1.1', str(re1))


@ways
def f2():
    cursor.execute("select industry from 大数据职位")
    re = cursor.fetchall()
    cursor.execute("select number from 大数据职位")
    num = cursor.fetchall()
    a = {}
    for i in range(0, len(re)):
        x = re[i][0].split(',')
        for k in range(0, len(x)):
            x[k] = x[k].strip()
            if x[k] in a:
                a[x[k]] += int(num[i][0])
            else:
                a[x[k]] = int(num[i][0])
    b = []
    for key, value in a.items():
        b.append([value, key])
    b.sort(reverse=True)
    hy = [x[1] for x in b[:10]]
    n = [x[0] for x in b[:10]]
    conf.set('chart', 'chart.2.1', str(hy))
    conf.set('chart', 'chart.2.2', str(n))


@ways
def f3():
    l1_1 = ['XXXX讲师', '项目开发经理', '`''技术/研发总监''`', '大数据开发工程师', '`''技术/研究/项目负责人''`', '服务器工程师', '数据库工程师', '软件开发工程师',
            '建模工程师', '硬件工程师', '网络工程师', '人工智能开发工程师', '后端工程师', '机器学习工程师']
    l2_1 = ['`''数据挖掘/分析/处理工程师''`', '数据管理工程师', 'Web前端工程师', '`''计算机维修/维护工程师''`']
    l3_1 = ['Java工程师', '`''C++工程师''`', 'PHP工程师', '`''C#工程师''`', '`''.NET工程师''`', 'Hadoop工程师', 'Python工程师', 'Perl工程师',
            'Ruby工程师', 'Nodejs工程师', 'Go工程师', 'Javascript工程师', 'Delphi工程师', 'jsp工程师', 'sql工程师', 'Linux开发工程师',
            'Android开发工程师', 'IOS开发工程师', '`''GIS开发/研发工程师''`', 'BI工程师']
    l = l1_1 + l2_1 + l3_1
    x = []
    city = ['上海', '深圳', '广州', '北京', '武汉', '成都', '杭州', '南京', '西安', '苏州']
    v = []
    for i in l:
        sql = "select number from " + str(i) + " where id <'10000'"
        cursor.execute(sql)
        re = cursor.fetchall()
        re = np.array(re)
        re = re.astype(int)
        if len(re) == 0:
            continue
        else:
            s = re.sum()
        v.append([s, i])
    v.sort(reverse=True)
    l = []
    for i in range(10):
        l.append(v[i][1])
    for i in l:
        sql = "select place,number from " + str(i) + " where id <'10000'"
        cursor.execute(sql)
        re = cursor.fetchall()
        re = list(re)
        if len(re) == 0:
            continue
        a = {}
        for j in range(0, len(re)):
            if re[j][0] in city:
                if re[j][0] in a:
                    a[re[j][0]] += int(re[j][1])
                else:
                    a[re[j][0]] = int(re[j][1])
            else:
                continue
        for key, value in a.items():
            x.append([key, i, value])
    ct = set([w[0] for w in x])
    conf.set('chart', 'chart.3.1', str(ct))
    conf.set('chart', 'chart.3.2', str(l))
    conf.set('chart', 'chart.3.3', str(x))


@ways
def f4():
    cursor.execute("select place,ave_pay,number from qcwy where id < '10000'")
    re = cursor.fetchall()
    re = list(re)
    df = pd.DataFrame(re)
    df = df.dropna()
    df.columns = ["place", "ave_pay", 'number']
    df['number'] = df['number'].astype(int)
    a = df.groupby('place').mean().sort_values(by='ave_pay', ascending=False)
    a = list(a.index)
    a1 = df.groupby('place')['number']
    a2 = df.groupby('place')['ave_pay']
    b = []
    c = [a[i] for i in range(len(a)) if a[i].find('省') == -1 and a[i] != '台湾' and a[i] != '吉林' and a[i] != '国外' \
         and a[i] != '宣城' and a[i] != '新疆' and a[i] != '池州' and a[i] != '燕郊开发区' \
         and a[i] != '黔西南']
    # 取出前十的城市及薪资
    for i in c:
        v = a1.get_group(i).values
        u = a2.get_group(i).values
        for j in range(0, len(v)):
            u[j] = u[j] * v[j]
        u = np.array(u)
        v = np.array(v)
        s = round(u.sum() / v.sum(), 2)
        b.append([s, i])
    b.sort(reverse=True)
    x = [w[1] for w in b[:10]]
    y = [w[0] for w in b[:10]]
    conf.set('chart', 'chart.4.1', str(x))
    conf.set('chart', 'chart.4.2', str(y))


@ways
def f5():
    cursor.execute("select place,number from 大数据职位")
    re = cursor.fetchall()
    # 将数据存入 dataframe 中
    re = list(re)
    df = pd.DataFrame(re)
    df.columns = ['place', 'num']
    df['num'] = df['num'].astype('int')
    # 用groupby函数进行分组，求和，排序
    a = df.groupby('place').sum().sort_values(by='num', ascending=False)
    # 取出前十的城市及需求量
    b = [x for x in a.values[:10]]
    c = [x for x in a.index[:10]]
    # 将数组一维化
    b = np.array(b)
    b = b.ravel()
    b = list(b)
    conf.set('chart', 'chart.5.1', str(c))
    conf.set('chart', 'chart.5.2', str(b))


@ways
def f6():
    # 将学历，经验，薪水提出来                                     分析了10000条数据
    cursor.execute("select education,experience,ave_pay,number from qcwy where id <'10000'")
    re = cursor.fetchall()
    # 转化为dataframe
    re = list(re)
    df = pd.DataFrame(re)
    df = df.dropna()
    df.columns = ['education', 'experience', 'ave_pay', 'number']
    # df['experience'] = df['experience'].astype(int)
    df['number'] = df['number'].astype(int)
    df['experience'] = df['experience'].astype(int)
    # 分组
    q = df.groupby(['education', 'experience'])['ave_pay']
    x = df.groupby(['education', 'experience'])['number']
    # p = df.groupby('education')['ave_pay'].mean()
    # 获得所有的经验值
    w = df.groupby('experience')['ave_pay'].mean()
    # 只选了需求较多的几个学历
    p = ['', '中专', '大专', '本科', '硕士']
    # 经验，排序
    w = list(w.index)
    w.sort()
    t = []
    for i in p:
        for j in w:
            try:
                v = q.get_group((i, j)).values
                u = x.get_group((i, j)).values
                for k in range(0, len(v)):
                    v[k] = v[k] * u[k]
                s = v.sum() / u.sum()
                v = round(s, 2)
                j = str(j) + '年'
                if i == '':
                    t.append(['不限', j, v])
                else:
                    t.append([i, j, v])
            except:
                pass
    for i in range(0, len(w)):
        w[i] = str(w[i]) + '年'
    p[0] = '不限'
    conf.set('chart', 'chart.6.1', str(p))
    conf.set('chart', 'chart.6.2', str(w))
    conf.set('chart', 'chart.6.3', str(t))


@ways
def f7():
    cursor.execute("select education,ave_pay,number from qcwy where id < '10000'")
    re = cursor.fetchall()
    df = pd.DataFrame(list(re))
    df = df.dropna()
    df.columns = ['education', 'pay', 'num']
    df['num'] = df['num'].astype(int)
    a = df.groupby('education')['pay']
    b = df.groupby('education')['num']
    c = df.groupby('education')['num'].sum()
    x = []
    y = []
    z = []
    for i in list(c.index):
        v = a.get_group(i).values
        w = np.array(b.get_group(i).values)
        s = 0
        for j in range(0, len(v)):
            s += v[j] * int(w[j])
        x.append(i)
        y.append(round(s / w.sum(), 2))
        z.append(w.sum())
    x[x.index('')] = '不限'
    conf.set('chart', 'chart.7.1', str(x))
    conf.set('chart', 'chart.7.2', str(y))
    conf.set('chart', 'chart.7.3', str(z))


@ways
def f8():
    cursor.execute("select city,ave_pay from qlrc")
    re = cursor.fetchall()
    df = pd.DataFrame(list(re))
    df.columns = ['city', 'pay']
    df = df.dropna()
    df['pay'] = df['pay'].astype(float)
    a = df.groupby('city')['pay'].mean().sort_values(ascending=False)
    city = [list(a.index)[i] for i in range(10)]
    pay = [round(list(a.values)[i], 2) for i in range(10)]
    conf.set('chart', 'chart.8.1', str(city))
    conf.set('chart', 'chart.8.2', str(pay))


@ways
def f9():
    cursor.execute("select city from qlrc ")
    re = cursor.fetchall()
    df = pd.DataFrame(list(re))
    df.columns = ['city']
    df = df.dropna()
    a = df.groupby('city')['city'].count()
    conf.set('chart', 'chart.9.1', str(list(a.index)))
    conf.set('chart', 'chart.9.2', str(list(a.values)))


@ways
def f10():
    cursor.execute("select experience,education ,number from 传统职业 where id <'10000'")
    re = cursor.fetchall()
    df1 = pd.DataFrame(list(re))
    df1 = df1.dropna()
    # 学历
    df1.columns = ['experience', 'education', 'number']
    df1['number'] = df1['number'].astype(int)
    q = df1.groupby('education')['number'].sum()
    a = ['', '中专', '大专', '本科', '硕士']
    k = list(q.index)
    b = [k[i] for i in range(0, len(k)) if k[i] in a]
    try:
        b[b.index('')] = '不限'
    except:
        pass
    c = [q.values[i] for i in range(0, len(k)) if k[i] in a]
    cursor.execute("select experience,education ,number from 新兴职业 where id <'10000'")
    re = cursor.fetchall()
    df2 = pd.DataFrame(list(re))
    df2 = df2.dropna()
    df2.columns = ['experience', 'education', 'number']
    df2['number'] = df2['number'].astype(int)
    q = df2.groupby('education')['number'].sum()
    k = list(q.index)
    d = [k[i] for i in range(0, len(k)) if k[i] in a]
    try:
        d[d.index('')] = '不限'
    except:
        pass
    f = [q.values[i] for i in range(0, len(k)) if k[i] in a]
    p1 = df1.groupby('experience')['number'].sum()
    k = list(p1.index)
    for i in range(0, len(k)):
        k[i] = str(k[i]) + '年'
    p2 = df2.groupby('experience')['number'].sum()
    j = list(p2.index)
    for i in range(0, len(j)):
        j[i] = str(j[i]) + '年'
    conf.set('chart', 'chart.10.1', str(b))
    conf.set('chart', 'chart.10.2', str(c))
    conf.set('chart', 'chart.10.3', str(d))
    conf.set('chart', 'chart.10.4', str(f))
    conf.set('chart', 'chart.10.5', str(k))
    conf.set('chart', 'chart.10.6', str(list(p1.values)))
    conf.set('chart', 'chart.10.7', str(j))
    conf.set('chart', 'chart.10.8', str(list(p2.values)))


@ways
def f11():
    l1_1 = ['XXXX讲师', '项目开发经理', '`''技术/研发总监''`', '大数据开发工程师', '`''技术/研究/项目负责人''`', '服务器工程师', '数据库工程师', '软件开发工程师',
            '建模工程师', '硬件工程师', '网络工程师', '人工智能开发工程师', '后端工程师', '机器学习工程师']
    l2_1 = ['`''数据挖掘/分析/处理工程师''`', '数据管理工程师', 'Web前端工程师', '`''计算机维修/维护工程师''`']
    l3_1 = ['Java工程师', '`''C++工程师''`', 'PHP工程师', '`''C#工程师''`', '`''.NET工程师''`', 'Hadoop工程师', 'Python工程师', 'Perl工程师',
            'Ruby工程师', 'Nodejs工程师', 'Go工程师', 'Javascript工程师', 'Delphi工程师', 'jsp工程师', 'sql工程师', 'Linux开发工程师',
            'Android开发工程师', 'IOS开发工程师', '`''GIS开发/研发工程师''`', 'BI工程师']
    l = l1_1 + l2_1 + l3_1
    a = []
    for i in l:
        sql = "select experience,number from " + str(i) + " where id <'10000'"
        # print(sql)
        cursor.execute(sql)
        re = cursor.fetchall()
        re = list(re)
        if len(re) == 0:
            continue
        # print(re)
        c = np.array(list(map(lambda x: int(x[0]) * int(x[1]), re)))
        d = np.array([int(x[1]) for x in re])
        c = c.astype(int)
        d = d.astype(int)
        s = round(c.sum() / d.sum(), 2)
        a.append([s, i])
    a.sort(reverse=True)
    x = [a[i][1] for i in range(10)]
    y = [a[i][0] for i in range(10)]
    conf.set('chart', 'chart.11.1', str(x))
    conf.set('chart', 'chart.11.2', str(y))


@ways
def f12():
    cursor.execute("select experience,ave_pay,number from qcwy where id < '10000'")
    re = cursor.fetchall()
    df = pd.DataFrame(list(re))
    df = df.dropna()
    df.columns = ['experience', 'pay', 'num']
    df['num'] = df['num'].astype(int)
    a = df.groupby('experience')['pay']
    b = df.groupby('experience')['num']
    c = df.groupby('experience')['num'].sum()
    data = []
    for i in list(c.index):
        v = a.get_group(i).values
        w = np.array(b.get_group(i).values)
        s = 0
        for j in range(0, len(v)):
            s += v[j] * int(w[j])
        data.append([i, w.sum(), round(s / w.sum(), 2)])
    conf.set('chart', 'chart.12.1', str(data))


@ways
def f13():
    l1_1 = ['XXXX讲师', '项目开发经理', '`''技术/研发总监''`', '大数据开发工程师', '`''技术/研究/项目负责人''`', '服务器工程师', '数据库工程师', '软件开发工程师',
            '建模工程师', '硬件工程师', '网络工程师', '人工智能开发工程师', '后端工程师', '机器学习工程师']
    l2_1 = ['`''数据挖掘/分析/处理工程师''`', '数据管理工程师', 'Web前端工程师', '`''计算机维修/维护工程师''`']
    l3_1 = ['Java工程师', '`''C++工程师''`', 'PHP工程师', '`''C#工程师''`', '`''.NET工程师''`', 'Hadoop工程师', 'Python工程师', 'Perl工程师',
            'Ruby工程师', 'Nodejs工程师', 'Go工程师', 'Javascript工程师', 'Delphi工程师', 'jsp工程师', 'sql工程师', 'Linux开发工程师',
            'Android开发工程师', 'IOS开发工程师', '`''GIS开发/研发工程师''`', 'BI工程师']
    l = l1_1 + l2_1 + l3_1
    x = []
    y = []
    z = []
    for i in l:
        sql = "select ave_pay,number from " + str(i) + " where id <'10000'"
        # print(sql)
        cursor.execute(sql)
        re = cursor.fetchall()
        re = list(re)
        if len(re) == 0:
            continue
        re1 = [x for x in re if x[0] is not None]
        c = np.array(list(map(lambda x: float(x[0]) * int(x[1]), re1)))
        d = np.array([x[1] for x in re1])
        c = c.astype(float)
        d = d.astype(float)
        s = round(c.sum() / d.sum(), 2)
        x.append(i)
        y.append(s)
        z.append(d.sum())
    conf.set('chart', 'chart.13.1', str(x))
    conf.set('chart', 'chart.13.2', str(y))
    conf.set('chart', 'chart.13.3', str(z))


@ways
def f14():
    l1 = ['软件开发', '人工智能', '`''深度\机器学习''`', '前端', '后端', '数据', '算法', '游戏',
          '测试', '安全', '运维', 'UI', '区块链', '网络', '全栈', '硬件', '物联网']
    a = {}
    for i in l1:
        sql = "select ave_pay,number from  " + str(i) + " where id <'10000'"
        cursor.execute(sql)
        re = cursor.fetchall()
        re = list(re)
        if len(re) == 0:
            continue
        re1 = [x for x in re if x[0] is not None]
        c = np.array(list(map(lambda x: x[0] * int(x[1]), re1)))
        d = np.array([int(x[1]) for x in re1])
        s = round(c.sum() / d.sum(), 2)
        a[i] = s
    list_words = []
    for key, value in a.items():
        list_words.append([value, key])
    list_words.sort(reverse=True)
    q = [x[0] for x in list_words[:10]]
    p = [x[1] for x in list_words[:10]]
    conf.set('chart', 'chart.14.1', str(p))
    conf.set('chart', 'chart.14.2', str(q))


@ways
def f15():
    l1_1 = ['XXXX讲师', '项目开发经理', '`''技术/研发总监''`', '大数据开发工程师', '`''技术/研究/项目负责人''`', '服务器工程师', '数据库工程师', '软件开发工程师',
            '建模工程师', '硬件工程师', '网络工程师', '人工智能开发工程师', '后端工程师', '机器学习工程师']
    l2_1 = ['`''数据挖掘/分析/处理工程师''`', '数据管理工程师', 'Web前端工程师', '`''计算机维修/维护工程师''`']
    l3_1 = ['Java工程师', '`''C++工程师''`', 'PHP工程师', '`''C#工程师''`', '`''.NET工程师''`', 'Hadoop工程师', 'Python工程师', 'Perl工程师',
            'Ruby工程师', 'Nodejs工程师', 'Go工程师', 'Javascript工程师', 'Delphi工程师', 'jsp工程师', 'sql工程师', 'Linux开发工程师',
            'Android开发工程师', 'IOS开发工程师', '`''GIS开发/研发工程师''`', 'BI工程师']
    l = l1_1 + l2_1 + l3_1
    a = []
    for i in l:
        sql = "select ave_pay,number from " + str(i) + " where id <'10000'"
        cursor.execute(sql)
        re = cursor.fetchall()
        re = list(re)
        if len(re) == 0:
            continue
        re1 = [x for x in re if x[0] is not None]
        c = np.array(list(map(lambda x: float(x[0]) * int(x[1]), re1)))
        d = np.array([int(x[1]) for x in re1])
        s = round(c.sum() / d.sum(), 2)
        a.append([s, i])
    a.sort(reverse=True)
    x = [x[1] for x in a[:10]]
    y = [x[0] for x in a[:10]]
    conf.set('chart', 'chart.15.1', str(x))
    conf.set('chart', 'chart.15.2', str(y))


@ways
def f16():
    cursor.execute("select place,number from qcwy ")
    re = cursor.fetchall()
    re = list(re)
    df = pd.DataFrame(re)
    df = df.dropna()
    df.columns = ['place', 'num']
    df['num'] = df['num'].astype('int')
    # 根据place进行分组，根据num进行排序
    w = df.groupby('place').sum().sort_values(by='num', ascending=False)
    z = np.array(w.values)
    z = z.ravel()
    w = list(w.index)
    c = [w[i] for i in range(10)]
    d = [z[i] for i in range(10)]
    conf.set('chart', 'chart.16.1', str(c))
    conf.set('chart', 'chart.16.2', str(d))


@ways
def f17():
    x = []
    l1 = ['Java工程师', 'C++工程师', 'PHP工程师', 'C#工程师', '.NET工程师', 'Hadoop工程师', 'Python工程师', 'Perl工程师', 'Ruby工程师',
          'Nodejs工程师', 'Go工程师', 'Javascript工程师', 'Delphi工程师', 'jsp工程师', 'sql工程师', 'Linux开发工程师', 'Android开发工程师',
          'IOS开发工程师', 'GIS开发/研发工程师', 'BI工程师']
    for i in l1:
        j = '`' + i + '`'  # SKILL_PAY
        sql = "SELECT AVE_PAY,NUMBER FROM " + j + ""
        cursor.execute(sql)
        re = cursor.fetchall()
        re = list(re)
        if len(re) == 0:
            continue
        re1 = [x for x in re if x[0] is not None]
        c = np.array(list(map(lambda x: float(x[0]) * int(x[1]), re1)))
        d = np.array([int(x[1]) for x in re1])
        s = round(c.sum() / d.sum(), 2)
        x.append([s, i])
    x.sort(reverse=True)
    jn = [a[1] for a in x[:10]]
    mo = [a[0] for a in x[:10]]
    conf.set('chart', 'chart.17.1', str(jn))
    conf.set('chart', 'chart.17.2', str(mo))


@ways
def f18():
    cursor.execute("select title,number from 新兴职业 where id <'10000'")
    re = cursor.fetchall()
    a = ['学习', '人工智能', '数据', '区块链', '算法', '物联网', '视觉', '自然语言']
    re = list(re)
    df = pd.DataFrame(re)
    df.columns = ['job', 'number']
    df['number'] = df['number'].astype(int)
    b = {}
    s = 0
    for i in range(0, df.shape[0] - 1):
        s += df.ix[i, 'number']
        for j in a:
            if df.ix[i, 'job'].find(j) != -1:
                if j in b:
                    b[j] += df.ix[i, 'number']
                else:
                    b[j] = df.ix[i, 'number']
    job = []
    num = []
    for key, value in b.items():
        key = key.replace('数据', '大数据')
        key = key.replace('学习', '机器学习')
        key = key.replace('视觉', '机器视觉')
        job.append(key)
        num.append(round(value / s, 2))
    conf.set('chart', 'chart.18.1', str(job))
    conf.set('chart', 'chart.18.2', str(num))


@ways
def f19():
    l1 = ['华为', '搜狐', '滴滴出行', '金山', '新浪', '美团', '甲骨文', '阿里巴巴', '微软', '爱奇艺', '中国电信', '百度', '奇虎', '英特尔', '完美世界', '字节跳动',
          '中国移动', '中国联通', '巨人网络', '浪潮', '联想', '搜狗', '蚂蚁金服', '腾讯', '网易', '小米', 'IBM', '京东']
    l2 = ['初中及以下', '中专、技校、中技', '高中', '大专', '本科', '硕士', '博士', '不限']
    a = {}
    for i in l1:
        j = '`' + i + '`'
        sql = "SELECT COUNT(*) FROM " + j + ""
        cursor.execute(sql)
        results = cursor.fetchone()
        count1 = []
        for m in l2:
            if m == '初中及以下':
                sql = "SELECT ID FROM " + j + "where (education like  '%初中%')"
                cursor.execute(sql)
                result = cursor.fetchall()
                num = len(result)
                num = int(num)
                count1.append(num)
            elif m == '中专、技校、中技':
                sql = "SELECT ID FROM " + j + "where (education like '%中专%' or education like '%技校%' or education like '%中技%')"
                cursor.execute(sql)
                result = cursor.fetchall()
                num = len(result)
                num = int(num)
                count1.append(num)
            else:
                sql = "SELECT ID FROM " + j + "where (education like '%" + m + "%')"
                cursor.execute(sql)
                result = cursor.fetchall()
                num = len(result)
                num = int(num)
                count1.append(num)
        a[i] = count1
    data = []
    for key, value in a.items():
        data.append(value)
    conf.set('chart', 'chart.19.1', str(l1))
    conf.set('chart', 'chart.19.2', str(l2))
    conf.set('chart', 'chart.19.3', str(data))


@ways
def f20():
    l1 = ['华为', '搜狐', '滴滴出行', '金山', '新浪', '美团', '甲骨文', '阿里巴巴', '微软', '爱奇艺', '中国电信', '百度', '奇虎', '英特尔', '完美世界', '字节跳动',
          '中国移动', '中国联通', '巨人网络', '浪潮', '联想', '搜狗', '蚂蚁金服', '腾讯', '网易', '小米', 'IBM', '京东']
    x = []
    y = []
    g = []
    for i in l1:
        j = '`' + i + '`'  # SKILL_PAY
        sql = "SELECT AVE_PAY FROM " + j + "where( AVE_PAY not like '面议')"
        cursor.execute(sql)
        results = cursor.fetchall()
        results = list(results)
        results = pd.DataFrame(results)
        results = results.dropna()
        if len(results) == 0:
            continue
        results.columns = ['PAY']
        results[['PAY']] = results[['PAY']].astype(float)
        ave = results['PAY'].mean()
        ave = float(ave)
        ave = round(ave, 2)
        sql = "SELECT experience2 FROM " + j + ""
        cursor.execute(sql)
        results = cursor.fetchall()
        results = list(results)
        results = pd.DataFrame(results)
        results.columns = ['EXP']
        results[['EXP']] = results[['EXP']].astype(float)
        ave3 = results['EXP'].mean()
        ave3 = round(ave3, 2)
        x.append(ave)
        y.append(ave3)
        g.append(i)
    conf.set('chart', 'chart.20.1', str(g))
    conf.set('chart', 'chart.20.2', str(x))
    conf.set('chart', 'chart.20.3', str(y))


@ways
def f21():
    l1 = ['华为', '搜狐', '滴滴出行', '金山', '新浪', '美团', '甲骨文', '阿里巴巴', '微软', '爱奇艺', '中国电信', '百度', '奇虎', '英特尔', '完美世界', '字节跳动',
          '中国移动', '中国联通', '巨人网络', '浪潮', '联想', '搜狗', '蚂蚁金服', '腾讯', '网易', '小米', 'IBM', '京东']
    a = {}
    for i in l1:
        j = '`' + i + '`'
        sql = "SELECT welfare FROM " + j + ""
        cursor.execute(sql)
        re = list(cursor.fetchall())
        for i in re:
            if i[0] == '':
                continue
            q = i[0].replace('[', '')
            q = q.replace(']', '')
            x = q.split(',')
            for k in x:
                k = k.replace("'", '')
                if k in a:
                    a[k] += 1
                else:
                    a[k] = 1
    x = []
    y = []
    for key, values in a.items():
        x.append(key)
        y.append(values)
    db.close()
    conf.set('chart', 'chart.21.1', str(x))
    conf.set('chart', 'chart.21.2', str(y))
