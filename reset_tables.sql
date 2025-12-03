truncate files_to_import;
truncate rs41_header;
truncate rs41_data;
truncate rs92_header;
truncate rs92_data;
truncate rs11g_header;
truncate rs11g_data;
truncate ims100_header;
truncate ims100_data;

alter sequence rs41_header_report_id_seq restart;
alter sequence rs41_data_observation_id_seq restart;
alter sequence rs92_header_report_id_seq restart;
alter sequence rs92_data_observation_id_seq restart;
alter sequence rs11g_header_report_id_seq restart;
alter sequence rs11g_data_observation_id_seq restart;
alter sequence ims100_header_report_id_seq restart;
alter sequence ims100_data_observation_id_seq restart;
