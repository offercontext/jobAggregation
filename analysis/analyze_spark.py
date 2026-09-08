from pyspark.sql import SparkSession
from pyspark.sql import functions as F

spark = SparkSession.builder.appName('myapp').getOrCreate() 

def f1():
  df1 = spark.sql("SELECT ave_pay, number FROM 传统职业 WHERE id < '10000'")
  re1 = df1.rdd.map(lambda x: (x.ave_pay, x.number)).collect()

  m = [x[0] for x in re1 if x[0] is not None and x[1] is not None]
  n = [int(x[1]) for x in re1 if x[0] is not None and x[1] is not None]
  
  a = []
  for i in range(len(m)):
    for j in range(n[i]):
      a.append(m[i])
      
  a = spark.sparkContext.parallelize(a)
  a1 = a.filter(lambda x: x != a.max() and x != a.min()).collect()

  df2 = spark.sql("SELECT ave_pay, number FROM 新兴职业 WHERE id < '10000'") 
  re2 = df2.rdd.map(lambda x: (x.ave_pay, x.number)).collect()

  m = [x[0] for x in re2 if x[0] is not None and x[1] is not None]
  n = [int(x[1]) for x in re2 if x[0] is not None and x[1] is not None]
  
  a = []
  for i in range(len(m)):
    for j in range(n[i]):
      a.append(m[i])

  a = spark.sparkContext.parallelize(a) 
  b1 = a.filter(lambda x: x != a.max() and x != a.min()).collect()

  q = [a1, b1]
  re1 = # prepare boxplot data

  conf.set('chart', 'chart.1.1', str(re1))

def f2():
  df = spark.sql("SELECT industry, number FROM 大数据职位")
  
  re = df.rdd.map(lambda x: (x.industry, x.number)).collect()

  a = {}
  for i in range(len(re)):
    industries = re[i][0].split(',')
    for industry in industries:
      industry = industry.strip()
      if industry in a:
        a[industry] += int(re[i][1])  
      else:
        a[industry] = int(re[i][1])
        
  b = []
  for k, v in a.items():
    b.append([v, k])
  
  b.sort(key=lambda x: x[0], reverse=True)

  hy = [x[1] for x in b[:10]]
  n = [x[0] for x in b[:10]]

  conf.set('chart', 'chart.2.1', str(hy))
  conf.set('chart', 'chart.2.2', str(n))

def f3():

  l1_1 = ['XXXX讲师', '项目开发经理', '`技术/研发总监`', '大数据开发工程师', '`技术/研究/项目负责人`', 
          '服务器工程师', '数据库工程师', '软件开发工程师', '建模工程师', '硬件工程师', 
          '网络工程师', '人工智能开发工程师', '后端工程师', '机器学习工程师']
          
  l2_1 = ['`数据挖掘/分析/处理工程师`', '数据管理工程师', 'Web前端工程师', '`计算机维修/维护工程师`']
  
  l3_1 = ['Java工程师', '`C++工程师`', 'PHP工程师', '`C#工程师`', '`_.NET工程师`', 'Hadoop工程师', 
          'Python工程师', 'Perl工程师','Ruby工程师', 'Nodejs工程师', 'Go工程师', 'Javascript工程师',
          'Delphi工程师', 'jsp工程师', 'sql工程师', 'Linux开发工程师', 'Android开发工程师', 
          'IOS开发工程师', '`GIS开发/研发工程师`', 'BI工程师']
          
  l = l1_1 + l2_1 + l3_1

  jobs_df = spark.createDataFrame(l,StringType()).withColumnRenamed('value', 'job')

  city = ['上海', '深圳', '广州', '北京', '武汉', '成都', '杭州', '南京', '西安', '苏州']

  for job in l:
    number_df = spark.sql("SELECT number FROM {} WHERE id < '10000'".format(job))
    number = number_df.rdd.map(lambda x: x[0]).sum()

    city_df = spark.sql("SELECT place, number FROM {} WHERE id < '10000'".format(job)) 
    city_number = city_df.where(F.col("place").isin(city)).groupBy("place").sum("number")

    city_number_pandas = city_number.toPandas()

    for i in range(len(city_number_pandas)):
      city = city_number_pandas.iloc[i][0]
      number = city_number_pandas.iloc[i][1]
      x.append([city, job, number])

  ct = set([w[0] for w in x])  
  conf.set('chart', 'chart.3.1', str(ct))

  top_jobs = jobs_df.join(number_df, on=['job'], how='inner')\
                  .sort(F.desc("sum(number)"))\
                  .limit(10)\
                  .select("job")\
                  .collect()
  
  conf.set('chart', 'chart.3.2', str([j.job for j in top_jobs]))

  conf.set('chart', 'chart.3.3', str(x))

def f4():

  df = spark.sql("SELECT place, ave_pay, number FROM qcwy WHERE id < '10000'")

  df = df.dropna()

  df = df.withColumn("number", df["number"].cast("int"))

  df = df.groupBy("place").avg("ave_pay", "number")

  df = df.sort(F.desc("avg(ave_pay)"))

  top_cities = df.limit(10).select("place").collect()

  top_cities = [c.place for c in top_cities if c.place.find('省')== -1 and c.place != '台湾']

  ave_pays = []
  for city in top_cities[:10]:
    pays = df.filter("place = '{}'".format(city))\
             .select(F.expr("avg(ave_pay) * avg(number)")).collect()
    ave_pays.append(pays[0][0])

  conf.set('chart', 'chart.4.1', str(top_cities[:10])) 
  conf.set('chart', 'chart.4.2', str([round(p,2) for p in ave_pays]))
  
def f5():

  df = spark.sql("SELECT place, number FROM 大数据职位")

  top10 = df.groupBy("place").sum("number").sort(F.desc("sum(number)")).limit(10)

  cities = [r.place for r in top10.select("place").collect()]
  numbers = [r[0] for r in top10.select(F.sum("number")).collect()]

  conf.set('chart', 'chart.5.1', str(cities))
  conf.set('chart', 'chart.5.2', str(numbers))


def f6():

  df = spark.sql("SELECT education, experience, ave_pay, number FROM qcwy WHERE id < '10000'")

  df = df.na.drop()

  df = df.withColumn("number", df["number"].cast("int"))
  df = df.withColumn("experience", df["experience"].cast("int"))

  pays = df.groupBy("education", "experience").avg("ave_pay")
  nums = df.groupBy("education", "experience").sum("number")

  edu_levels = ['', '中专', '大专', '本科', '硕士']

  experiences = df.select("experience").distinct().rdd.map(lambda r: r[0]).collect()
  experiences.sort()
  experiences = [str(e) + "年" for e in experiences]

  data = []
  for edu in edu_levels:
    for exp in experiences:
      try:
        pay = pays.filter("education='{}' AND experience={}".format(edu, exp)).collect()[0][0]
        num = nums.filter("education='{}' AND experience={}".format(edu, exp)).collect()[0][0]
        avg_pay = round(pay * num / num, 2)
        data.append([edu if edu else "不限", exp, avg_pay])  
      except:
        pass

  edu_levels[0] = "不限"

  conf.set('chart', 'chart.6.1', str(edu_levels))
  conf.set('chart', 'chart.6.2', str(experiences)) 
  conf.set('chart', 'chart.6.3', str(data))

def f7():
  
  df = spark.sql("SELECT education, ave_pay, number FROM qcwy WHERE id < '10000'")

  df = df.na.drop()

  df = df.withColumn("number", df["number"].cast("int"))

  edu_pays = df.groupBy("education").avg("ave_pay")
  edu_nums = df.groupBy("education").sum("number")
  
  educations = [r.education for r in edu_nums.select("education").collect()]
  pays = [r[0] for r in edu_pays.collect()]
  nums = [r[0] for r in edu_nums.collect()]

  for i in range(len(educations)):
    if educations[i] == '':
      educations[i] = '不限'
    pays[i] = round(pays[i] * nums[i] / nums[i], 2)

  conf.set('chart', 'chart.7.1', str(educations))
  conf.set('chart', 'chart.7.2', str(pays))
  conf.set('chart', 'chart.7.3', str(nums))
  
def f8():

  df = spark.sql("SELECT city, ave_pay FROM qlrc")

  df = df.na.drop()
  df = df.withColumn("ave_pay", df["ave_pay"].cast("float"))
  
  df = df.groupBy("city").avg("ave_pay").sort(F.desc("avg(ave_pay)"))

  cities = [r.city for r in df.limit(10).select("city").collect()]
  pays = [round(r[0],2) for r in df.limit(10).select(F.avg("ave_pay")).collect()]

  conf.set('chart', 'chart.8.1', str(cities))
  conf.set('chart', 'chart.8.2', str(pays))
  
def f9():

  df = spark.sql("SELECT city FROM qlrc")
  df = df.na.drop()

  counts = df.groupBy("city").count()

  cities = [r.city for r in counts.select("city").collect()]
  counts = [r.count for r in counts.select("count").collect()]
  
  conf.set('chart', 'chart.9.1', str(cities))
  conf.set('chart', 'chart.9.2', str(counts))


def f10():

  df1 = spark.sql("SELECT experience, education, number FROM 传统职业 WHERE id < '10000'")
  df1 = df1.na.drop()
  df1 = df1.withColumn("number", df1["number"].cast("int"))

  edu_nums1 = df1.groupBy("education").sum("number")
  edu_levels = ['', '中专', '大专', '本科', '硕士']
  educations1 = [r.education for r in edu_nums1.select("education").collect() if r.education in edu_levels]
  educations1[educations1.index('')] = '不限'
  nums1 = [r[0] for r in edu_nums1.select("sum(number)").collect() if r.education in edu_levels]

  df2 = spark.sql("SELECT experience, education, number FROM 新兴职业 WHERE id < '10000'")
  df2 = df2.na.drop()
  df2 = df2.withColumn("number", df2["number"].cast("int"))

  edu_nums2 = df2.groupBy("education").sum("number")
  educations2 = [r.education for r in edu_nums2.select("education").collect() if r.education in edu_levels]
  educations2[educations2.index('')] = '不限'
  nums2 = [r[0] for r in edu_nums2.select("sum(number)").collect() if r.education in edu_levels]

  exp_nums1 = df1.groupBy("experience").sum("number")
  experiences1 = [str(r.experience)+"年" for r in exp_nums1.select("experience").collect()]
  exp_nums1 = [r[0] for r in exp_nums1.select("sum(number)").collect()]

  exp_nums2 = df2.groupBy("experience").sum("number")
  experiences2 = [str(r.experience)+"年" for r in exp_nums2.select("experience").collect()]
  exp_nums2 = [r[0] for r in exp_nums2.select("sum(number)").collect()]

  conf.set('chart', 'chart.10.1', str(educations1))
  conf.set('chart', 'chart.10.2', str(nums1))
  conf.set('chart', 'chart.10.3', str(educations2))
  conf.set('chart', 'chart.10.4', str(nums2))
  conf.set('chart', 'chart.10.5', str(experiences1))
  conf.set('chart', 'chart.10.6', str(exp_nums1))
  conf.set('chart', 'chart.10.7', str(experiences2))
  conf.set('chart', 'chart.10.8', str(exp_nums2))

  
def f11():

  jobs = ['XXXX讲师', '项目开发经理', '`技术/研发总监`', '大数据开发工程师',  
          '`技术/研究/项目负责人`', '服务器工程师', '数据库工程师', '软件开发工程师', 
          '建模工程师', '硬件工程师', '网络工程师', '人工智能开发工程师', '后端工程师', '机器学习工程师',
          '`数据挖掘/分析/处理工程师`', '数据管理工程师', 'Web前端工程师', '`计算机维修/维护工程师`',
          'Java工程师','`C++工程师`', 'PHP工程师','`C#工程师`','`.NET工程师`', 'Hadoop工程师',  
          'Python工程师', 'Perl工程师', 'Ruby工程师', 'Nodejs工程师', 'Go工程师', 
          'Javascript工程师','Delphi工程师', 'jsp工程师','sql工程师', 'Linux开发工程师',
          'Android开发工程师','IOS开发工程师','`GIS开发/研发工程师`', 'BI工程师']

  data = []  
  for job in jobs:
    df = spark.sql("SELECT experience, number FROM {} WHERE id < '10000'".format(job))
    df = df.na.drop()
    df = df.withColumn("number", df["number"].cast("int"))
    nums = df.groupBy().agg(F.sum(F.col("number") * F.col("experience")))
    exp = nums.collect()[0][0] / nums.agg(F.sum("number")).collect()[0][0]
    data.append([job, round(exp,2)])

  data.sort(key=lambda x: x[1], reverse=True)

  top_jobs = [x[0] for x in data[:10]]
  exps = [x[1] for x in data[:10]]

  conf.set('chart', 'chart.11.1', str(top_jobs))
  conf.set('chart', 'chart.11.2', str(exps))

def f12():

  df = spark.sql("SELECT experience, ave_pay, number FROM qcwy WHERE id < '10000'") 
  df = df.na.drop()
  df = df.withColumn("number", df["number"].cast("int"))

  exp_pays = df.groupBy("experience").avg("ave_pay")
  exp_nums = df.groupBy("experience").sum("number")

  experiences = [r.experience for r in exp_nums.select("experience").collect()]
  nums = [r[0] for r in exp_nums.select("sum(number)").collect()]
  pays = [r[0] for r in exp_pays.select("avg(ave_pay)").collect()]

  data = []
  for i in range(len(experiences)):
     data.append([experiences[i], nums[i], round(pays[i] * nums[i] / nums[i], 2)])

  conf.set('chart', 'chart.12.1', str(data))
