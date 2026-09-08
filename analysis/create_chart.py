import os
import sys
import pyecharts as p
import configparser
import random

from pyecharts import Style

script_path = os.path.realpath(__file__)
script_dir = os.path.dirname(script_path)
sys.path.append(script_dir)

import analysis_main as A


# 函数注册器
def ways(func):
    A.Analyze.chart_fn_list.append(func)

    def wrapper(*args, **kw):
        return func(*args, **kw)

    return wrapper


# 图表参数迭代器
def parameter(fn):
    name = fn.__name__.replace('t', '')
    for i in range(1, 50):
        pa = 'chart.' + name + '.' + str(i)
        yield eval(conf_chart[pa])


def main():
    global conf_chart
    conf = configparser.ConfigParser()
    conf.read('./conf/conf.ini')
    conf_chart = conf['chart']
    p.configure(global_theme='macarons')  # 设置主题
    charts = []
    # print(A.Analyze.chart_fn_list)
    for fn in A.Analyze.chart_fn_list:
        pa = parameter(fn)
        x = fn(pa)
        x.width = '100%'
        if fn.__name__ == 't3':
            x.width = 650
            x.height = 500
        if fn.__name__ == 't12':
            x.width = 700
            x.height = 500
        if fn.__name__ == 't21':
            x.width = 700
            x.height = 500

        charts.append(x)
        # grid.add(fn(pa))
        # print(fn.__name__ + '  ok')

    return charts


@ways
def t1(pa):
    y = next(pa)
    print(y[0])
    box = p.Boxplot('新兴与传统职业薪水对比')
    box.add('传统职业', ['薪水'], [y[0]], is_toolbox_show=False)
    box.add('新兴职业', ['薪水'], [y[1]], is_toolbox_show=False)

    return box


@ways
def t2(pa):
    bar = p.Bar('需求前10的行业')

    bar.add('需求', next(pa), next(pa), is_toolbox_show=False)
    return bar


@ways
def t3(pa):
    hm = p.HeatMap('地区职位与需求关系', width=1500, height=600)
    hm.add("需求量", next(pa), next(pa), next(pa), is_visualmap=True, visual_range=[350, 25000],
           visual_text_color="#000", visual_orient='horizontal', yaxis_label_textsize=8,
           yaxis_rotate=-45, is_toolbox_show=False)

    return hm


@ways
def t4(pa):
    bar = p.Bar("薪资前10城市")
    bar.add("薪资", next(pa), next(pa), mark_line=["average"], is_toolbox_show=False)
    return bar


@ways
def t5(pa):
    bar = p.Bar("大数据职位需求前10城市")
    bar.add("需求量", next(pa), next(pa), mark_line=["average"], is_toolbox_show=False)
    return bar


@ways
def t6(pa):
    bar3d = p.Bar3D("学历经验与薪水关系", width=1200, height=500)
    range_color = ['#313695', '#4575b4', '#74add1', '#abd9e9', '#e0f3f8', '#ffffbf',
                   '#fee090', '#fdae61', '#f46d43', '#d73027', '#a50026']
    bar3d.add("学历经验薪水", next(pa), next(pa), next(pa),
              is_visualmap=True, visual_range=[4000, 30000],
              visual_range_color=range_color, grid3d_width=150, grid3d_depth=80,
              is_grid3d_rotate=True, grid3d_shading='realistic', grid3d_rotate_speed=30, is_toolbox_show=False)
    return bar3d


@ways
def t7(pa):
    scatter = p.Scatter("学历与需求量、薪水关系")
    scatter.add("薪水", next(pa), next(pa), extra_data=next(pa), is_visualmap=True,
                xaxis_type="category", visual_dimension=2, visual_range=[500, 500000]
                , is_toolbox_show=False, visual_top=9999)
    return scatter


@ways
def t8(pa):
    bar = p.Bar("山东薪水前10的城市排名")
    bar.add("薪水", next(pa), next(pa), mark_line=['average'], is_toolbox_show=False)
    return bar


@ways
def t9(pa):
    # geo地图有不显示数值的bug加上下面的函数和  add的参数 label_formatter=label_formatter
    def label_formatter(params):
        return params.value[2]

    style = p.Style(
        title_color="#fff",
        title_pos="center",
        width=1200,
        height=600,
        background_color='#404a59'
    )
    chart = p.Geo("山东省计算机职位分布", '数据来自齐鲁人才网，部分地区数据不准确', **style.init_style, subtitle_text_size=18)
    city = [i.replace('市', '') for i in next(pa)]
    # label_formatter=label_formatter防bug maptype去掉就是全国地图
    chart.add("", city, next(pa), maptype='山东', visual_range=[0, 700], label_formatter=label_formatter,
              visual_text_color="#fff", is_legend_show=True,
              symbol_size=15, is_visualmap=True,
              tooltip_formatter='{b}',
              label_emphasis_textsize=15,
              label_emphasis_pos='right', is_toolbox_show=False)
    return chart


@ways
def t10(pa):
    pie = p.Pie("新兴与传统职业学历经验需求对比", width=700, height=400)
    pie.add("传统学历需求", next(pa), next(pa),
            radius=[50, 55], center=[35, 53])
    pie.add("新兴学历需求", next(pa), next(pa),
            radius=[0, 45], center=[35, 53], rosetype='radius',
            is_random=True)
    pie.add("传统经验需求", next(pa), next(pa),
            radius=[50, 55], center=[70, 53])
    pie.add("新兴经验需求", next(pa), next(pa),
            radius=[0, 45], center=[70, 53], rosetype='radius',
            legend_orient='vertical',
            legend_pos='left', legend_top='center', is_random=True, is_toolbox_show=False)
    return pie


@ways
def t11(pa):
    bar = p.Bar('经验要求前十的职位')
    bar.add('经验', next(pa), next(pa), mark_line=["average"], is_toolbox_show=False)
    return bar


@ways
def t12(pa):
    range_color = [
        '#313695', '#4575b4', '#74add1', '#abd9e9', '#e0f3f8', '#ffffbf',
        '#fee090', '#fdae61', '#f46d43', '#d73027', '#a50026']
    scatter3D = p.Scatter3D("经验需求薪水")
    scatter3D.add("3D", next(pa), is_visualmap=True, visual_range_color=range_color, is_grid3d_rotate=True,
                  visual_range=[7000, 35000]
                  , xaxis3d_name='经验', yaxis3d_name='需求', zaxis3d_name='薪水', is_toolbox_show=False, visual_top=9999)
    return scatter3D


@ways
def t13(pa):
    scatter = p.EffectScatter("职位与需求量、薪水关系")
    next(pa)
    y = next(pa)
    y2 = next(pa)
    for i in range(len(y)):
        scatter.add("薪水", [y[i]], [y2[i]], is_toolbox_show=False, symbol_size=15,
                    effect_scale=3, effect_period=3, symbol=random.choice(['roundRect', 'pin']),
                    effect_brushtype='fill')

    return scatter


@ways
def t14(pa):
    bar = p.Bar('计算机专业薪水前10方向')
    y = next(pa)
    y_values = next(pa)
    # bar.add("薪水", mark_line=['average'],is_toolbox_show=False)
    for i in range(len(y) - 4):
        bar.add(y[i], ['薪水'], [y_values[i]], is_show=True, is_toolbox_show=False, is_label_show=True,
                label_formatter='{a}')
    bar.add(y[-1], ['薪水'], [y_values[-1]], is_legend_show=False, is_toolbox_show=False, is_label_show=True,
            label_formatter='{a}')
    # bar.add('方向', next(pa), next(pa), is_stack=False, mark_line=["average"], is_toolbox_show=False)
    return bar


@ways
def t15(pa):
    bar = p.Bar('薪水前十的职位')
    bar.add('薪水', next(pa), next(pa), mark_line=["average"], is_toolbox_show=False)
    return bar


@ways
def t16(pa):
    bar = p.Bar('计算机专业需求前十城市')
    bar.add('需求量', next(pa), next(pa), mark_line=['average'], is_toolbox_show=False)
    return bar


@ways
def t17(pa):
    bar = p.Bar("高薪水技能")
    y = next(pa)
    y_values = next(pa)
    # bar.add("薪水", mark_line=['average'],is_toolbox_show=False)
    for i in range(len(y) - 4):
        if i % 3 == 1:
            bar.add(y[i], ['薪水'], [y_values[i]], is_show=True, mark_line=['average'], is_toolbox_show=False)
        else:
            bar.add(y[i], ['薪水'], [y_values[i]], is_show=True, is_toolbox_show=False)

    return bar


@ways
def t18(pa):
    shape = ("path://M367.855,428.202c-3.674-1.385-7.452-1.966-11.146-1"
             ".794c0.659-2.922,0.844-5.85,0.58-8.719 c-0.937-10.407-7."
             "663-19.864-18.063-23.834c-10.697-4.043-22.298-1.168-29.9"
             "02,6.403c3.015,0.026,6.074,0.594,9.035,1.728 c13.626,5."
             "151,20.465,20.379,15.32,34.004c-1.905,5.02-5.177,9.115-9"
             ".22,12.05c-6.951,4.992-16.19,6.536-24.777,3.271 c-13.625"
             "-5.137-20.471-20.371-15.32-34.004c0.673-1.768,1.523-3.423"
             ",2.526-4.992h-0.014c0,0,0,0,0,0.014 c4.386-6.853,8.145-14"
             ".279,11.146-22.187c23.294-61.505-7.689-130.278-69.215-153"
             ".579c-61.532-23.293-130.279,7.69-153.579,69.202 c-6.371,"
             "16.785-8.679,34.097-7.426,50.901c0.026,0.554,0.079,1.121,"
             "0.132,1.688c4.973,57.107,41.767,109.148,98.945,130.793 c58."
             "162,22.008,121.303,6.529,162.839-34.465c7.103-6.893,17.826"
             "-9.444,27.679-5.719c11.858,4.491,18.565,16.6,16.719,28.643 "
             "c4.438-3.126,8.033-7.564,10.117-13.045C389.751,449.992,"
             "382.411,433.709,367.855,428.202z")
    liquid = p.Liquid("各新兴职业所占比例")
    liquid.add(next(pa), next(pa), is_liquid_outline_show=False, shape=shape, is_toolbox_show=False)
    return liquid


@ways
def t19(pa):
    # def label_formatter(params):
    #     print(233)
    #     print(params.value[2])
    #     return params.value[2]
    l1 = next(pa)
    pp = next(pa)
    data = next(pa)
    pie = p.Pie('大公司学历要求')
    style = Style()
    pie_style = style.add(
        is_label_show=True,
        label_pos="center",
        is_label_emphasis=False,
        label_formatter='{b}',
        label_text_size=16,
        is_legend_show=False,
        label_text_color="#000"
        # label_text_color=None
    )
    for i in range(len(data)):
        data[i][0] = 0
        data[i][1] = 0

    for i in range(len(l1)):
        l1[i] = [l1[i], '', '', '', '', '', '', '']
    pie.add('', l1[0], data[0], center=[10, 25], radius=[13, 18],
            **pie_style)
    pie.add('', l1[1], data[1], center=[20, 25], radius=[13, 18],
            legend_pos='left', **pie_style)
    pie.add('', l1[2], data[2], center=[30, 25], radius=[13, 18],
            **pie_style)
    pie.add('', l1[3], data[3], center=[40, 25], radius=[13, 18],
            **pie_style)
    pie.add('', l1[4], data[4], center=[50, 25], radius=[13, 18],
            **pie_style)
    pie.add('', l1[5], data[5], center=[60, 25], radius=[13, 18],
            **pie_style)
    pie.add('', l1[6], data[6], center=[70, 25], radius=[13, 18],
            **pie_style)
    pie.add('', l1[7], data[7], center=[80, 25], radius=[13, 18],
            **pie_style)
    pie.add('', l1[8], data[8], center=[10, 53], radius=[13, 18],
            **pie_style)
    pie.add('', l1[9], data[9], center=[20, 53], radius=[13, 18],
            **pie_style)
    pie.add('', l1[10], data[10], center=[30, 53], radius=[13, 18],
            **pie_style)
    pie.add('', l1[11], data[11], center=[40, 53], radius=[13, 18],
            **pie_style)
    pie.add('', l1[12], data[12], center=[50, 53], radius=[13, 18],
            **pie_style)
    pie.add('', l1[13], data[13], center=[60, 53], radius=[13, 18],
            **pie_style)
    pie.add('', l1[14], data[14], center=[70, 53], radius=[13, 18],
            **pie_style)
    pie.add('', l1[15], data[15], center=[80, 53], radius=[13, 18],
            **pie_style)
    pie.add('', l1[16], data[16], center=[90, 25], radius=[13, 18],
            **pie_style)
    pie.add('', l1[17], data[17], center=[90, 53], radius=[13, 18],
            **pie_style)
    pie.add('', l1[18], data[18], center=[10, 80], radius=[13, 18],
            **pie_style)
    pie.add('', l1[19], data[19], center=[20, 80], radius=[13, 18],
            **pie_style)
    pie.add('', l1[20], data[20], center=[30, 80], radius=[13, 18],
            **pie_style)
    pie.add('', l1[21], data[21], center=[40, 80], radius=[13, 18],
            **pie_style)
    pie.add('', l1[22], data[22], center=[50, 80], radius=[13, 18],
            **pie_style)
    pie.add('', l1[23], data[23], center=[60, 80], radius=[13, 18],
            **pie_style)
    pie.add('', l1[24], data[24], center=[70, 80], radius=[13, 18],
            **pie_style)
    pie.add('', l1[25], data[25], center=[80, 80], radius=[13, 18],
            **pie_style)
    pie.add('', l1[26], data[26], center=[90, 80], radius=[13, 18],
            **pie_style, is_toolbox_show=False)

    return pie


@ways
def t20(pa):
    scatter = p.Scatter('各大公司工资经验')
    scatter.add('工资', next(pa), next(pa), extra_data=next(pa), is_visualmap=True, visual_dimension=2,
                xaxis_type="category", visual_range=[0, 6], is_toolbox_show=False
                )
    return scatter


@ways
def t21(pa):
    wordcloud = p.WordCloud('大公司福利', width=1300, height=620)
    wordcloud.add("", next(pa), next(pa), word_size_range=[20, 100], is_toolbox_show=False)
    return wordcloud
