import pymysql
import pyecharts as p


def find(name):
    user = "root"
    password = "zym233521"
    db = pymysql.connect(host="localhost", user=user, password=password, charset="utf8")
    cursor = db.cursor()
    cursor.execute('USE `ujn_a`;')
    names = name + 's'

    cursor.execute('DROP VIEW IF EXISTS {};'.format(names))
    sql_1 = 'CREATE VIEW {} AS SELECT qcwy.xexperience,qcwy.education FROM qcwy WHERE title like "%{}%" '.format(names,
                                                                                                                 name)
    cursor.execute(sql_1)

    cursor.execute("select count(*) from " + names)
    alls = cursor.fetchone()[0]

    list1 = ['本科', '硕士', '博士']
    list_edu = []
    list_edu_sql = ["select count(*) from {} where education = '{}';".format(names, i) for i in list1]
    for i in list_edu_sql:
        cursor.execute(i)
        list_edu.append(cursor.fetchone()[0])

    list2 = ['无工作经验', '1年经验', '2年经验', '3-4年经验', '5-7年经验', '8-9年经验', '10年以上经验']
    list_exp = []
    list_exp_sql = ["select count(*) from {} where xexperience = '{}';".format(names, i) for i in list2]
    for i in list_exp_sql:
        cursor.execute(i)
        list_exp.append(cursor.fetchone()[0])

    p.configure(global_theme='macarons')  # 设置主题

    bing = p.Pie()
    attr = list2
    v1 = list_exp
    bing.add("", attr, v1, is_label_show=True, is_toolbox_show=False, legend_top='bottom')
    bing.render('static/html/bing.html')

    list4 = []
    for i in range(3):
        qiu = p.Liquid(title=list1[i], title_pos='center', title_text_size=30, title_top='80%', width=600, height=400)
        list3 = [round(list_edu[i] / alls, 2)]
        list4.extend(list3)
        qiu.add(list1[i], list3, is_liquid_animation=True, is_toolbox_show=False,
                liquid_color=['#21bbff', '#00b6ff', '#23c4ff', '#47c7ff'], is_liquid_outline_show=False)
        qiu.render('static/html/qiu{}.html'.format(i + 1))

    # 返回

    n = 1
    e_all = 0
    for i in list_exp[1:]:
        e_all = e_all + i / alls / 21 * n
        n = n + 1

    n = 1
    d_all = 0
    for i in list_edu[1:]:
        d_all = d_all + i / alls / 2 * n
        n = n + 1

    if e_all > d_all:
        if e_all - d_all <= 0.06:
            result = '考研和直接就业都很不错呢'
        else:
            result = '建议您毕业后直接就业哦'
    else:
        result = '建议您准备考研哦'

    if name == '学习': name = '机器学习'

    back = [name, result]
    return back

    return


if __name__ == '__main__':
    find('软件')
