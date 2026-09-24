-- Metin2 AI QA test hesapları — YALNIZCA QA/DEV veritabanında çalıştırın, production'da ASLA.
--
-- Hesaplar (account.account) burada oluşturulur. Karakterler ise ilk seferde QA client ile
-- normal "karakter oluştur" ekranından yaratılmalıdır (gerçek oyuncu yolu).
-- Karakter adı hesap adıyla aynı olmalı (ör. AI_QA_001) — sunucudaki IsQaBot() adın
-- "AI_QA_" önekine bakar.
--
-- Şifre: orchestrator QA_ACCOUNT_PASSWORD ortam değişkeninden okur (qa.toml [accounts].password).
-- Aşağıdaki 'CHANGE_ME' değerini değiştirin.
--
-- Not: klasik şema MySQL PASSWORD() hash'i kullanır. MySQL 8 / MariaDB'nin bazı sürümlerinde
-- PASSWORD() yoktur; o durumda auth sunucunuzun kullandığı hash ile değiştirin
-- (ör. CONCAT('*', UPPER(SHA1(UNHEX(SHA1('CHANGE_ME')))))).

USE account;

INSERT INTO account (login, password, social_id, email, status, create_time)
VALUES
  ('AI_QA_001',     PASSWORD('CHANGE_ME'), '1234567', 'qa@localhost', 'OK', NOW()),
  ('AI_QA_002',     PASSWORD('CHANGE_ME'), '1234567', 'qa@localhost', 'OK', NOW()),
  ('AI_QA_WARRIOR', PASSWORD('CHANGE_ME'), '1234567', 'qa@localhost', 'OK', NOW()),
  ('AI_QA_SURA',    PASSWORD('CHANGE_ME'), '1234567', 'qa@localhost', 'OK', NOW()),
  ('AI_QA_SHAMAN',  PASSWORD('CHANGE_ME'), '1234567', 'qa@localhost', 'OK', NOW()),
  ('AI_QA_NINJA',   PASSWORD('CHANGE_ME'), '1234567', 'qa@localhost', 'OK', NOW())
ON DUPLICATE KEY UPDATE status = 'OK';
