// Tracker验证工具
// 用于验证tracker JSON文件的格式和有效性

const fs = require('fs');
const path = require('path');

// 定义必要的字段和验证规则
const requiredFields = ['name', 'url', 'tracker', 'last_updated', 'status', 'type'];
const validStatuses = ['active', 'inactive'];
const validTypes = ['private', 'semi_private', 'public'];

/**
 * 验证单个tracker文件
 * @param {string} filePath - 文件路径
 * @returns {Object} 验证结果
 */
function validateTrackerFile(filePath) {
  try {
    const content = fs.readFileSync(filePath, 'utf8');
    const tracker = JSON.parse(content);
    const errors = [];
    
    // 检查必要字段
    requiredFields.forEach(field => {
      if (!tracker[field]) {
        errors.push(`缺少必要字段: ${field}`);
      }
    });
    
    // 验证status字段
    if (tracker.status && !validStatuses.includes(tracker.status)) {
      errors.push(`无效的status值: ${tracker.status}, 有效值为: ${validStatuses.join(', ')}`);
    }
    
    // 验证type字段
    if (tracker.type && !validTypes.includes(tracker.type)) {
      errors.push(`无效的type值: ${tracker.type}, 有效值为: ${validTypes.join(', ')}`);
    }
    
    // 验证URL格式
    if (tracker.url) {
      try {
        new URL(tracker.url);
      } catch (e) {
        errors.push(`无效的URL格式: ${tracker.url}`);
      }
    }
    
    if (tracker.tracker) {
      try {
        new URL(tracker.tracker);
      } catch (e) {
        errors.push(`无效的tracker URL格式: ${tracker.tracker}`);
      }
    }
    
    return {
      file: filePath,
      valid: errors.length === 0,
      errors
    };
  } catch (error) {
    return {
      file: filePath,
      valid: false,
      errors: [`解析错误: ${error.message}`]
    };
  }
}

/**
 * 验证目录中的所有tracker文件
 * @param {string} dirPath - 目录路径
 * @returns {Array} 验证结果数组
 */
function validateTrackerDirectory(dirPath) {
  const results = [];
  
  try {
    const files = fs.readdirSync(dirPath);
    
    files.forEach(file => {
      if (file.endsWith('.json')) {
        const filePath = path.join(dirPath, file);
        const result = validateTrackerFile(filePath);
        results.push(result);
      }
    });
  } catch (error) {
    console.error(`读取目录错误: ${error.message}`);
  }
  
  return results;
}

// 主验证函数
function validateAllTrackers() {
  console.log('开始验证tracker文件...');
  
  const trackerTypes = ['private', 'semi_private', 'public'];
  let allResults = [];
  
  trackerTypes.forEach(type => {
    const dirPath = path.join(__dirname, '..', 'trackers', type);
    console.log(`\n验证 ${type} trackers...`);
    const results = validateTrackerDirectory(dirPath);
    allResults = allResults.concat(results);
  });
  
  // 输出结果统计
  const validCount = allResults.filter(r => r.valid).length;
  const invalidCount = allResults.length - validCount;
  
  console.log('\n验证结果汇总:');
  console.log(`总文件数: ${allResults.length}`);
  console.log(`有效文件: ${validCount}`);
  console.log(`无效文件: ${invalidCount}`);
  
  // 输出无效文件详情
  if (invalidCount > 0) {
    console.log('\n无效文件详情:');
    allResults.forEach(result => {
      if (!result.valid) {
        console.log(`\n文件: ${result.file}`);
        result.errors.forEach(err => console.log(`  - ${err}`));
      }
    });
    
    process.exit(1);
  } else {
    console.log('\n所有tracker文件验证通过!');
  }
}

// 如果直接运行此脚本，则执行验证
if (require.main === module) {
  validateAllTrackers();
}

module.exports = {
  validateTrackerFile,
  validateTrackerDirectory,
  validateAllTrackers
};