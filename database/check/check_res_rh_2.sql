--step 1
select table_name from data_summary where max_res_rh >= 1000 and max_res_rh is not null;

/*
 table_name
-------------
 data_201310
 data_201311
 data_201401
 data_201402
 data_201411
 data_201707
 data_201802
 data_201807
 data_201811
 data_201905
 data_202003
 data_202106
 data_202207
 data_202208
 */

--step 2
select distinct g_product_id into ema from data_201310 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_201311 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_201401 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_201402 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_201411 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_201707 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_201802 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_201807 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_201811 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_201905 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_2020035 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_202003 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_202106 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_202207 where res_rh >= 1000 and res_rh is not null;
insert into ema select distinct g_product_id from data_202208 where res_rh >= 1000 and res_rh is not null;

--step 3
select g_product_id, idstation_pk, report_timestamp, g_site_key, g_general_sitecode, g_Instrument_Type, g_MainSonde_ModelFamily
into ema_2
from header where g_product_id in (select * from ema);


