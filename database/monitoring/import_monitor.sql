SELECT
    sonde,
    COUNT(CASE WHEN date_of_import IS NULL AND note IS NULL THEN 1 END) AS to_import,
    COUNT(CASE WHEN date_of_import IS NOT NULL THEN 1 END) AS imported,
    COUNT(CASE WHEN note IS NOT NULL THEN 1 END) AS excluded,
    COUNT(*) AS total
FROM files_to_import
GROUP BY 1
ORDER BY 1;